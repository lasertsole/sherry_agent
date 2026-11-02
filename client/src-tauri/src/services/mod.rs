//! Service layer modules.
//!
//! | Module | Responsibility |
//! |--------|---------------|
//! | [`python_bridge`] | HTTP bridge to the Python backend (REST + SSE) |
//! | [`python_process`] | Python backend subprocess lifecycle management |
//! | [`backend_bundle`] | Locate and version-check the installer's bundled backend |
//! | [`download`] | Shared download progress/event/version primitives |
//! | [`cuda_detect`] | Host CUDA support probe (model settings tab gate) |
//! | [`cuda_pack_downloader`] | Optional CUDA pack download with HTTP Range resume |

pub mod backend_bundle;
pub mod cuda_detect;
pub mod cuda_pack_downloader;
pub mod download;
pub mod python_bridge;
pub mod python_process;
