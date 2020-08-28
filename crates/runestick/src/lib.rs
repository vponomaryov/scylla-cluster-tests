//! runestick, a simple stack-based virtual machine.
//!
//! ## Contributing
//!
//! If you want to help out, there's a number of optimization tasks available in
//! [Future Optimizations][future-optimizations].
//!
//! Create an issue about the optimization you want to work on and communicate that
//! you are working on it.
//!
//! ## Features of runestick
//!
//! * [Clean Rust FFI][rust-ffi].
//! * Stack-based C FFI like with Lua (TBD).
//! * Stack frames, allowing for isolation across function calls.
//! * A rust-like reference language called *Rune*.
//!
//! ## Rune Scripts
//!
//! runestick comes with a simple scripting language called *Rune*.
//!
//! You can run example scripts through rune-cli:
//!
//! ```bash
//! cargo run -- ./scripts/hello_world.rn
//! ```
//!
//! If you want to see diagnostics of your unit, you can do:
//!
//! ```bash
//! cargo run -- ./scripts/hello_world.rn --dump-unit --trace
//! ```
//!
//! [rust-ffi]: https://github.com/udoprog/runestick/blob/master/crates/runestick-http/src/lib.rs
//! [future-optimizations]: https://github.com/udoprog/runestick/blob/master/FUTURE_OPTIMIZATIONS.md

#![deny(missing_docs)]

mod any;
mod context;
mod value;
mod vm;
#[macro_use]
mod macros;
mod access;
mod bytes;
mod error;
mod future;
mod hash;
mod item;
pub mod packages;
mod panic;
mod reflection;
mod serde;
mod shared;
mod shared_ptr;
mod stack;
pub mod unit;

pub use crate::access::{Mut, NotAccessibleMut, NotAccessibleRef, Ref};
pub use crate::any::Any;
pub use crate::bytes::Bytes;
pub use crate::context::{Context, ContextError, Meta, MetaObject, MetaTuple, Module};
pub use crate::context::{
    ADD, ADD_ASSIGN, DIV, DIV_ASSIGN, FMT_DISPLAY, INDEX_GET, INDEX_SET, MUL, MUL_ASSIGN, NEXT,
    SUB, SUB_ASSIGN,
};
pub use crate::error::{Error, Result};
pub use crate::future::Future;
pub use crate::hash::Hash;
pub use crate::item::{Component, Item};
pub use crate::panic::Panic;
pub use crate::reflection::{
    FromValue, ReflectValueType, ToValue, UnsafeFromValue, UnsafeIntoArgs, UnsafeToValue,
};
pub use crate::shared::{RawStrongMutGuard, RawStrongRefGuard, Shared, StrongMut, StrongRef};
pub use crate::shared_ptr::SharedPtr;
pub use crate::stack::{Stack, StackError};
pub use crate::unit::{CompilationUnit, CompilationUnitError, Span};
pub use crate::value::{
    Object, RawValueMutGuard, RawValueRefGuard, TypedTuple, Value, ValueError, ValueType,
    ValueTypeInfo, VecTuple,
};
pub use crate::vm::{Inst, PanicReason, Task, TypeCheck, Vm, VmError};

mod collections {
    pub use hashbrown::HashMap;
    pub use hashbrown::HashSet;
}
