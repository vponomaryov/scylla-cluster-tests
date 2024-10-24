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

from typing import Optional, List, Union
from sdcm.remote.remote_base import StreamWatcher, Result
from unit_tests.lib.data_pickle import Pickler


class MockRemoter:
    """
    Example:
    >>> from unit_tests.lib.remoter_recorder import RemoterRecorder
    >>> import getpass
    >>> remoter = RemoterRecorder(hostname='127.0.0.1', user=getpass.getuser(), key_file='~/.ssh/scylla-qa-ec2')
    >>> remoter_result = remoter.run('echo "Do something with remoter"')
    >>> remoter.save_responses_to_file('/tmp/test1_remoter.json')
    >>> mock_remoter = MockRemoter('/tmp/test1_remoter.json')
    >>> mock_remoter_result = mock_remoter.run('echo "Do something with remoter"')
    >>> assert mock_remoter_result == remoter_result
    """
    user = 'scylla-test'

    def __init__(self, responses: Union[dict, str] = None):
        self.command_counter = {}
        if isinstance(responses, str):
            self.responses = Pickler.load_from_file(responses)
        elif isinstance(responses, dict):
            self.responses = responses

    def is_up(self, timeout=None):  # pylint: disable=unused-argument,no-self-use
        return True

    def _process_response(self, response):  # pylint: disable=no-self-use
        if isinstance(response, Result):
            return response
        elif isinstance(response, Exception):
            raise response
        return None

    # pylint: disable=too-many-arguments,unused-argument
    def run(self, cmd: str, timeout: Optional[float] = None,
            ignore_status: bool = False, verbose: bool = True, new_session: bool = False,
            log_file: Optional[str] = None, retry: int = 1, watchers: Optional[List[StreamWatcher]] = None,
            change_context: bool = False) -> Result:
        response = self.responses.get(cmd)
        if response is None:
            raise RuntimeError("Can't find response")
        output = self._process_response(response)
        if output is not None:
            return output
        if isinstance(response, list):
            try_number = self.command_counter.get(cmd, 0)
            if len(response) <= try_number:
                output = response[-1]
            else:
                output = response[try_number]
            self.command_counter[cmd] = try_number + 1
            return self._process_response(output)
        else:
            raise RuntimeError('Wrong response value, could be Result or Exception')

    # pylint: disable=too-many-arguments
    def sudo(self,
             cmd: str,
             timeout: Optional[float] = None,
             ignore_status: bool = False,
             verbose: bool = True,
             new_session: bool = False,
             log_file: Optional[str] = None,
             retry: int = 1,
             watchers: Optional[List[StreamWatcher]] = None,
             user: Optional[str] = 'root') -> Result:
        if user != self.user:
            if user == 'root':
                cmd = f"sudo {cmd}"
            else:
                cmd = f"sudo -u {user} {cmd}"
        return self.run(cmd=cmd,
                        timeout=timeout,
                        ignore_status=ignore_status,
                        verbose=verbose,
                        new_session=new_session,
                        log_file=log_file,
                        retry=retry,
                        watchers=watchers)
