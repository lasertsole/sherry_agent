//! Service layer modules.
//!
//! | Module | Responsibility |
//! |--------|---------------|
//! | [`python_bridge`] | HTTP bridge to the Python backend (REST + SSE) |
//! | [`python_process`] | Python backend subprocess lifecycle management |

pub mod python_bridge;
pub mod python_process;
