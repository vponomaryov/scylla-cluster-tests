# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright (c) 2020 ScyllaDB

import inspect
import logging
import threading
import time
from typing import Optional, Callable, Iterator, List
import yaml

import kubernetes as k8s
from invoke import Runner, Context, Config
from invoke.exceptions import ThreadException
from urllib3.exceptions import (
    MaxRetryError,
    ProtocolError,
    ReadTimeoutError,
)

from sdcm.cluster import TestConfig
from sdcm.utils.k8s import KubernetesOps
from sdcm.utils.common import deprecation
from sdcm.utils.decorators import retrying
from sdcm.wait import wait_for

from .base import RetryableNetworkException
from .remote_base import RemoteCmdRunnerBase, StreamWatcher

LOGGER = logging.getLogger(__name__)


class KubernetesRunner(Runner):
    read_timeout = 0.1

    def __init__(self, context: Context) -> None:
        super().__init__(context)

        self.process = None
        self._k8s_core_v1_api = KubernetesOps.core_v1_api(context.k8s_kluster.get_api_client())
        self._ws_lock = threading.RLock()

    def should_use_pty(self, pty: bool, fallback: bool) -> bool:
        return False

    def read_proc_output(self, reader: Callable[[int], str]) -> Iterator[str]:
        while self.process.is_open():
            yield reader(self.read_chunk_size)

    def read_proc_stdout(self, num_bytes: int) -> str:
        with self._ws_lock:
            return self.process.read_stdout(self.read_timeout)

    def read_proc_stderr(self, num_bytes: int) -> str:
        with self._ws_lock:
            return self.process.read_stderr(self.read_timeout)

    def _write_proc_stdin(self, data: str) -> None:
        with self._ws_lock:
            self.process.write_stdin(data)

    def close_proc_stdin(self) -> None:
        pass

    def start(self, command: str, shell: str, env: dict) -> None:
        with self._ws_lock:
            try:
                self.process = k8s.stream.stream(
                    self._k8s_core_v1_api.connect_get_namespaced_pod_exec,
                    name=self.context.config.k8s_pod,
                    container=self.context.config.k8s_container,
                    namespace=self.context.config.k8s_namespace,
                    command=[shell, "-c", command],
                    stderr=True,
                    stdin=True,
                    stdout=True,
                    tty=False,
                    _preload_content=False)
            except k8s.client.rest.ApiException as exc:
                raise ConnectionError(str(exc)) from None

    def kill(self) -> None:
        self.stop()

    @property
    def process_is_finished(self) -> bool:
        return not self.process.is_open()

    def returncode(self) -> Optional[int]:
        try:
            return self.process.returncode
        except (TypeError, KeyError, ):
            return None

    def stop(self) -> None:
        with self._ws_lock:
            if self.process:
                self.process.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


class KubernetesCmdRunner(RemoteCmdRunnerBase):
    exception_retryable = (ConnectionError, MaxRetryError, ThreadException)
    default_run_retry = 8

    def __init__(self, kluster, pod: str, container: Optional[str] = None, namespace: str = "default") -> None:
        self.kluster = kluster
        self.pod = pod
        self.container = container
        self.namespace = namespace

        super().__init__(hostname=f"{pod}/{container}")

    def get_init_arguments(self) -> dict:
        return {
            "kluster": self.kluster,
            "pod": self.pod,
            "container": self.container,
            "namespace": self.namespace,
        }

    @property
    def connection(self):
        raise RuntimeError('KubernetesCmdRunner does not hold any connection, use _create_connection instead')

    def is_up(self, timeout=None) -> bool:
        return True

    def _create_connection(self):
        return KubernetesRunner(Context(Config(overrides={"k8s_kluster": self.kluster,
                                                          "k8s_pod": self.pod,
                                                          "k8s_container": self.container,
                                                          "k8s_namespace": self.namespace, })))

    # pylint: disable=too-many-arguments
    def _run_execute(self, cmd: str, timeout: Optional[float] = None,  # pylint: disable=too-many-arguments
                     ignore_status: bool = False, verbose: bool = True, new_session: bool = False,
                     watchers: Optional[List[StreamWatcher]] = None):
        # TODO: This should be removed than sudo calls will be done in more organized way.
        tmp = cmd.split(maxsplit=3)
        if tmp[0] == 'sudo':
            deprecation("Using `sudo' in cmd string is deprecated.  Use `remoter.sudo()' instead.")
            frame = inspect.stack()[1]
            self.log.error("Cut off `sudo' from the cmd string: %s (%s:%s: %s)",
                           cmd, frame.filename, frame.lineno, frame.code_context[0].rstrip())
            if tmp[1] == '-u':
                cmd = tmp[3]
            else:
                cmd = cmd[cmd.find('sudo') + 5:]
        # Session should be created for each run
        return super()._run_execute(cmd, timeout=timeout, ignore_status=ignore_status, verbose=verbose,
                                    new_session=True, watchers=watchers)

    # pylint: disable=too-many-arguments,unused-argument
    @retrying(n=3, sleep_time=5, allowed_exceptions=(RetryableNetworkException, ))
    def receive_files(self, src, dst, delete_dst=False, preserve_perm=True, preserve_symlinks=False, timeout=300):
        KubernetesOps.copy_file(self.kluster, f"{self.namespace}/{self.pod}:{src}", dst,
                                container=self.container, timeout=timeout)
        return True

    # pylint: disable=too-many-arguments,unused-argument
    @retrying(n=3, sleep_time=5, allowed_exceptions=(RetryableNetworkException, ))
    def send_files(self, src, dst, delete_dst=False, preserve_symlinks=False, verbose=False):
        KubernetesOps.copy_file(self.kluster, src, f"{self.namespace}/{self.pod}:{dst}",
                                container=self.container, timeout=300)
        return True

    def _run_on_retryable_exception(self, exc: Exception, new_session: bool) -> bool:
        self.log.error(exc)
        if isinstance(exc, self.exception_retryable):
            raise RetryableNetworkException(str(exc), original=exc)
        return True

    def _close_connection(self):
        # Websocket connection is getting closed when run is ended, so nothing is needed to be done here
        pass

    def _open_connection(self):
        # Websocket connection is getting opened for each run, so nothing is needed to be done here
        pass

    def stop(self):
        # Websocket connection is getting closed when run is ended, so nothing is needed to be done here
        pass

    def _reconnect(self):
        # Websocket connection is getting closed when run is ended, so nothing is needed to be done here
        pass


class KubernetesPodWatcher(KubernetesRunner):
    """
    Pod template requirements:
    - Must have only one container
    - That the only container must have '${K8S_POD_COMMAND}' var as the container's command
      which gets run wrapped in the bash command.
    - It must have "${K8S_POD_NAME}" var as the pod name
    - It must have '${DOCKER_IMAGE_WITH_TAG}' var for the docker image that should be used

    All other variables for subsitution are customizible.
    """

    READ_REQUEST_TIMEOUT = 30
    POD_COUNTER_TO_LIVE = 300
    STREAM_CONNECTION_TTL = 7200

    def __init__(self, context: Context) -> None:
        super().__init__(context=context)
        self.current_read_bytes_num = 0
        self.pod_counter_to_live = self.POD_COUNTER_TO_LIVE
        self.stream_connection_start_time = time.time()

    @retrying(n=20, sleep_time=3, allowed_exceptions=(ConnectionError, ))
    def _open_stream(self) -> None:
        try:
            # NOTE: following API call is analog of the following CLI command:
            #   kubectl logs ... --follow=true --tail=-1
            self.process = self._k8s_core_v1_api.read_namespaced_pod_log(
                name=self.context.config.k8s_pod_name,
                namespace=self.context.config.k8s_namespace,
                follow=True,
                timestamps=False,
                # NOTE: need to set a timeout, because GKE's 'pod_log' API tends to hang
                _request_timeout=self.READ_REQUEST_TIMEOUT,
                _preload_content=False)
            self.stream_connection_start_time = time.time()
        except k8s.client.rest.ApiException as exc:
            LOGGER.warning(
                "'_open_stream()': failed to open pod log stream:\n%s", exc)
            # NOTE: following is workaround for the error 401 which may happen due to
            #       some config data corruption during the forced socket connection failure
            self._k8s_core_v1_api = KubernetesOps.core_v1_api(
                self.context.config.k8s_kluster.get_api_client())
            raise ConnectionError(str(exc)) from None

    @retrying(n=12, sleep_time=10, allowed_exceptions=(
        ProtocolError, ReadTimeoutError, TimeoutError, AttributeError))
    def _read_from_stream(self, num_bytes: int) -> str:
        pod_name = self.context.config.k8s_pod_name
        with self._ws_lock:
            if self.process.closed:
                LOGGER.warning(
                    "'_read_from_stream' called for the %s pod having closed socket. Recreating it.",
                    pod_name)
                self._start()
            elif self.STREAM_CONNECTION_TTL < time.time() - self.stream_connection_start_time:
                LOGGER.debug(
                    "'_read_from_stream': Recreate '%s' pod log stream connection to avoid freezes. "
                    "Reason: reached '%s' seconds of 'time to live'",
                    pod_name, self.STREAM_CONNECTION_TTL)
                self.process.close()
                self._start()

            result = self.process.read(num_bytes)
            if isinstance(result, bytes):
                return result.decode("utf-8")
            return result

    @retrying(n=30, sleep_time=10, allowed_exceptions=(
        ProtocolError, ReadTimeoutError, TimeoutError, AttributeError))
    def _start(self) -> None:
        pod_name = self.context.config.k8s_pod_name
        with self._ws_lock:
            LOGGER.debug("Calling 'runner._start()' method for the '%s' pod.\n", pod_name)
            self._open_stream()
            # NOTE: above API doesn't allow to specify starting bytes
            #       so, we should reread it.
            if self.current_read_bytes_num:
                LOGGER.debug(
                    "'runner._start()' method for the '%s' pod: "
                    "Going to re-read '%s' num of bytes to avoid sending bytes "
                    "to consumer which were already sent.",
                    pod_name, self.current_read_bytes_num)
                reread_bytes = 0
                while self.current_read_bytes_num > reread_bytes:
                    time.sleep(0.01)
                    self.process.read(self.read_chunk_size)
                    reread_bytes += self.read_chunk_size
                LOGGER.debug(
                    "'runner._start()' method for the '%s' pod: "
                    "Successfully re-read '%s' num of bytes.",
                    pod_name, self.current_read_bytes_num)

    def start(self, command: str, shell: str, env: dict) -> None:
        LOGGER.debug(
            "Calling 'runner.start()' method for the '%s' pod.\n"
            "command: %s\n",
            self.context.config.k8s_pod_name, command)
        self._start()

    def _get_docker_image(self) -> str:
        # TODO: parse 'command' and pick up proper image.
        #       Need to sync with the loaders-from-docker logic
        #       which is implemented for everything except c-s
        params = self.context.config.k8s_kluster.params
        return f"{params.get('docker_image')}:{params.get('scylla_version')}"

    def _get_pod_status(self) -> dict:
        result_raw = self.context.config.k8s_kluster.kubectl(
            f"get pod {self.context.config.k8s_pod_name} "
            "-o jsonpath='{.status}'",
            namespace=self.context.config.k8s_namespace).stdout.strip()
        return yaml.safe_load(result_raw) or {}

    def returncode(self, status: dict | None = None) -> Optional[int]:  # pylint: disable=arguments-differ
        if status is None:
            status = self._get_pod_status()
        # NOTE: logic is based on the following conditions:
        #   - status may be absent if container was not even created -> None
        #   - status.containerStatuses[0].state.running -> None
        #   - status.containerStatuses[0].state.terminated.exitCode -> read it
        if container_statuses := status.get("containerStatuses"):
            if state := container_statuses[0].get("state"):
                return (state.get("terminated") or state.get("running") or {}).get("exitCode")
        return None

    def _is_pod_ready_or_completed(self) -> bool:
        # NOTE: possible values for the 'type' field of a condition are following:
        #       PodScheduled, PodHasNetwork (alpha in v1.25), Initialized, ContainersReady, Ready
        #
        #       Expected success conditions for us are following:
        #         {"type": "Ready", "status": "False", "reason": "PodCompleted", ...}
        #       or
        #         {"type": "Ready", "status": "True", ...} # no 'reason' field if pod is running
        status, pod_name = self._get_pod_status(), self.context.config.k8s_pod_name
        returncode = self.returncode(status=status)
        if returncode not in (None, 0):
            raise RuntimeError(
                f"'{pod_name}' failed with following exit code: %s" % returncode)
        result = False
        for condition in status.get("conditions", []):
            if condition.get("type") != "Ready":
                continue
            result = condition.get("status") == "True" or condition.get("reason") == "PodCompleted"
        LOGGER.debug(
            "'runner._is_pod_ready_or_completed': %s , result: %s",
            pod_name, result)
        return result

    def _is_pod_failed_or_completed(self, _cache={}) -> bool:  # pylint: disable=dangerous-default-value
        last_call_at = _cache.get('last_call_at')
        if last_call_at and time.time() - last_call_at < 3:
            time.sleep(3)
        status = self._get_pod_status()
        _cache['last_call_at'] = time.time()
        if self.returncode(status=status) is not None:
            return True
        result = False
        for condition in status.get("conditions", []):
            if condition.get("type") != "Ready":
                continue
            result = condition.get("reason") in ("PodFailed", "PodCompleted")
        LOGGER.debug(
            "'runner._is_pod_failed_or_completed': %s , result: %s",
            self.context.config.k8s_pod_name, result)
        return result

    def read_proc_output(self, reader: Callable[[int], str]) -> Iterator[str]:
        while not self.process_is_finished:
            yield reader(self.read_chunk_size)

    def read_proc_stdout(self, num_bytes: int) -> str:
        # NOTE: 'pod logs reader driver' provides only combined output (stdout + stderr)
        with self._ws_lock:
            result = self._read_from_stream(num_bytes)
            self.current_read_bytes_num += num_bytes
            return result

    def read_proc_stderr(self, num_bytes: int) -> str:
        # NOTE: 'pod logs reader driver' provides only combined output (stdout + stderr)
        #       So, we do not try to read it having read everything as part of the stdout
        return ''

    def _write_proc_stdin(self, data: str) -> None:
        LOGGER.warning(
            "Unexpected operation on the 'K8S pod logs watcher' (%s) was attempted. "
            "Writing data to stdin cannot be done. Data: %s",
            self.context.config.k8s_pod_name, data)

    @property
    def process_is_finished(self) -> bool:
        if not self.process.closed:
            return False
        pod_failed_or_completed = self._is_pod_failed_or_completed()
        if not pod_failed_or_completed:
            self.pod_counter_to_live -= 1
        if self.pod_counter_to_live < 1:
            LOGGER.warning(
                "'process_is_finished': stopping '%s' pod because stream to it "
                "cannot be established having alive pod.",
                self.context.config.k8s_pod_name)
            self._stop_pod()
        return pod_failed_or_completed

    def _stop_pod(self) -> None:
        # NOTE: stop pod execution if pod is running, ignore error if it is not running
        self.context.config.k8s_kluster.kubectl(
            f"exec {self.context.config.k8s_pod_name} -- /bin/bash -c 'rm /tmp/keep_running'",
            namespace=self.context.config.k8s_namespace,
            timeout=30, ignore_status=True)
        self.process.close()

    def kill(self) -> None:
        LOGGER.warning(
            "'kill()' method is called for the '%s' pod",
            self.context.config.k8s_pod_name)
        self._stop_pod()

    def stop(self) -> None:
        if self._is_pod_failed_or_completed():
            LOGGER.warning(
                "'stop()' method is called for the '%s' pod, which is already closed. Ignoring",
                self.context.config.k8s_pod_name)
            return
        with self._ws_lock:
            if TestConfig().tester_obj().teardown_started:
                LOGGER.warning(
                    "'stop()' method is called for the '%s' pod as part of the 'tearDown'. "
                    "'log reader' socket is '%s'.",
                    self.context.config.k8s_pod_name, "closed" if self.process.closed else "open")
                self._stop_pod()
                raise InterruptedError(
                    "The '%s' pod execution was interrupted by the 'tearDown'")
            if self.process.closed:
                LOGGER.warning(
                    "'stop()' method is called for the '%s' pod, which is running "
                    "having closed 'log reader' socket. Which is probably the cause for "
                    "calling this method. Recreating the socket which can be "
                    "closed for various reasons unexpectedly.",
                    self.context.config.k8s_pod_name)
                self._start()
            else:
                LOGGER.warning(
                    "'stop()' method is called for the '%s' pod, "
                    "it is running and has open 'log reader' socket. "
                    "So, looks like it is intentional close of the socket.",
                    self.context.config.k8s_pod_name)
                self._stop_pod()

    def run(self, command, **kwargs):
        pod_name = self.context.config.k8s_pod_name

        LOGGER.debug(
            "'remoter.run': '%s' pod will be called with following command: "
            "%s\nAnd kwargs: %s\n",
            pod_name, command, kwargs)
        environ = self.context.config.k8s_environ
        environ["K8S_POD_COMMAND"] = command
        environ["K8S_POD_NAME"] = pod_name
        environ["DOCKER_IMAGE_WITH_TAG"] = self._get_docker_image()

        # NOTE: create loader pod and wait for it's readiness
        KubernetesOps.apply_file(
            kluster=self.context.config.k8s_kluster,
            config_path=self.context.config.k8s_template_path,
            modifiers=self.context.config.k8s_template_modifiers,
            namespace=self.context.config.k8s_namespace,
            environ=environ,
            envsubst=True,
        )
        wait_for(
            self._is_pod_ready_or_completed,
            step=2,
            text="'%s' pod is not ready/completed yet..." % pod_name,
            timeout=420,
            throw_exc=True)

        # NOTE: run a watcher for the pod's logs
        return super().run(command, **kwargs)


# pylint: disable=too-many-instance-attributes
class KubernetesPodRunner(KubernetesCmdRunner):
    def __init__(self, kluster,  # pylint: disable=too-many-arguments,super-init-not-called
                 template_path: str,
                 template_modifiers: list,
                 pod_name_template: str,
                 namespace: str,
                 environ: dict) -> None:
        self.kluster = kluster
        self.template_path = template_path
        self.template_modifiers = template_modifiers
        self.pod_name_template = pod_name_template
        self.namespace = namespace
        self.environ = environ
        RemoteCmdRunnerBase.__init__(  # pylint: disable=non-parent-init-called
            self=self, hostname=f"pod/{pod_name_template}")
        self._pod_counter = -1
        self._connections = []

    def stop(self):
        for connection in self._connections:
            connection.stop()

    @property
    def pod_name(self) -> str:
        LOGGER.debug(
            "'remoter.pod_name': called for the '%s'. Current counter value is '%s'",
            self.pod_name_template, self._pod_counter)
        return f"{self.pod_name_template}-{self._pod_counter}"

    def get_init_arguments(self) -> dict:
        return {
            "kluster": self.kluster,
            "template_path": self.template_path,
            "template_modifiers": self.template_modifiers,
            "pod_name": self.pod_name,
            "namespace": self.namespace,
            "environ": self.environ,
        }

    def _create_connection(self):
        # NOTE: we increase counter here because each new usage of the "KubernetesPodWatcher" class
        #       instance is triggered by the need to created a new pod with a unique name.
        self._pod_counter += 1
        connection = KubernetesPodWatcher(Context(Config(overrides={
            "k8s_kluster": self.kluster,
            "k8s_template_path": self.template_path,
            "k8s_template_modifiers": self.template_modifiers,
            "k8s_pod_name": self.pod_name,
            "k8s_namespace": self.namespace,
            "k8s_environ": self.environ,
        })))
        self._connections.append(connection)
        return connection

    # pylint: disable=too-many-arguments,unused-argument
    def receive_files(self, src, dst, delete_dst=False,
                      preserve_perm=True, preserve_symlinks=False, timeout=300):
        # TODO: may be implemented if we want to copy files which exist in the image
        #       because this runner doesn't imply execution of commands on the already
        #       running pod.
        raise NotImplementedError()

    # pylint: disable=too-many-arguments,unused-argument
    def send_files(self, src, dst, delete_dst=False, preserve_symlinks=False, verbose=False):
        # TODO: make this method create configmap with provided file and then
        #       add it to the template and mount it's files.
        raise NotImplementedError()
