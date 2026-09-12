use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyModule};
use snow::{Builder, HandshakeState, TransportState};

const SUITE: &str = "Noise_KK_25519_ChaChaPoly_SHA256";
const SESSION_PROFILE: &[u8] = b"ofca-companion-session/v1;agent-to-brain;no-early-data";
const HANDSHAKE_FRAME_MAX: usize = 4096;
const ABSOLUTE_TRANSPORT_PLAINTEXT_MAX: usize = 36_848;
const TAG_LEN: usize = 16;

create_exception!(ofca_native_snow, NoiseError, PyException);
create_exception!(ofca_native_snow, NoiseInputError, NoiseError);
create_exception!(ofca_native_snow, NoiseStateError, NoiseError);
create_exception!(ofca_native_snow, NoiseAuthenticationError, NoiseError);
create_exception!(ofca_native_snow, NoiseClosedError, NoiseError);

fn input_error() -> PyErr { NoiseInputError::new_err(()) }
fn state_error() -> PyErr { NoiseStateError::new_err(()) }
fn auth_error() -> PyErr { NoiseAuthenticationError::new_err(()) }
fn closed_error() -> PyErr { NoiseClosedError::new_err(()) }

fn valid_prologue(value: &[u8]) -> bool {
    value.len() == SESSION_PROFILE.len() + 1 + 32
        && value.starts_with(SESSION_PROFILE)
        && value[SESSION_PROFILE.len()] == 0
}

#[pyclass(module = "ofca_native_snow")]
struct NoiseResponder {
    handshake: Option<HandshakeState>,
    transport: Option<TransportState>,
    handshake_hash: Option<[u8; 32]>,
    first_message_consumed: bool,
    response_produced: bool,
    closed: bool,
}

impl NoiseResponder {
    fn fail_auth<T>(&mut self) -> PyResult<T> {
        self.close_internal();
        Err(auth_error())
    }

    fn fail_state<T>(&mut self) -> PyResult<T> {
        self.close_internal();
        Err(state_error())
    }

    fn close_internal(&mut self) {
        self.closed = true;
        self.handshake = None;
        self.transport = None;
        self.handshake_hash = None;
    }

    fn require_open(&self) -> PyResult<()> {
        if self.closed { Err(closed_error()) } else { Ok(()) }
    }
}

#[pymethods]
impl NoiseResponder {
    #[new]
    fn new(brain_private_key: &[u8], agent_public_key: &[u8], prologue: &[u8]) -> PyResult<Self> {
        if brain_private_key.len() != 32 || agent_public_key.len() != 32 || !valid_prologue(prologue) {
            return Err(input_error());
        }

        let mut local = [0u8; 32];
        local.copy_from_slice(brain_private_key);
        let params = SUITE.parse().map_err(|_| input_error())?;
        let built = Builder::new(params)
            .local_private_key(&local).map_err(|_| input_error())?
            .remote_public_key(agent_public_key).map_err(|_| input_error())?
            .prologue(prologue).map_err(|_| input_error())?
            .build_responder();
        local.fill(0);
        let handshake = built.map_err(|_| input_error())?;

        Ok(Self {
            handshake: Some(handshake),
            transport: None,
            handshake_hash: None,
            first_message_consumed: false,
            response_produced: false,
            closed: false,
        })
    }

    fn consume_first_handshake(&mut self, frame: &[u8]) -> PyResult<()> {
        self.require_open()?;
        if self.transport.is_some() || self.first_message_consumed || self.response_produced {
            return self.fail_state();
        }
        if frame.is_empty() || frame.len() > HANDSHAKE_FRAME_MAX {
            self.close_internal();
            return Err(input_error());
        }

        let handshake = match self.handshake.as_mut() {
            Some(value) => value,
            None => return self.fail_state(),
        };
        let mut payload = [0u8; HANDSHAKE_FRAME_MAX];
        match handshake.read_message(frame, &mut payload) {
            Ok(0) => { self.first_message_consumed = true; Ok(()) }
            Ok(_) | Err(_) => self.fail_auth(),
        }
    }

    fn produce_responder_handshake(&mut self, py: Python<'_>) -> PyResult<Py<PyBytes>> {
        self.require_open()?;
        if self.transport.is_some() || !self.first_message_consumed || self.response_produced {
            return self.fail_state();
        }
        let handshake = match self.handshake.as_mut() {
            Some(value) => value,
            None => return self.fail_state(),
        };
        let mut out = vec![0u8; HANDSHAKE_FRAME_MAX];
        let written = match handshake.write_message(&[], &mut out) {
            Ok(value) if value <= HANDSHAKE_FRAME_MAX => value,
            _ => return self.fail_auth(),
        };
        if !handshake.is_handshake_finished() { return self.fail_auth(); }
        let hash = handshake.get_handshake_hash();
        if hash.len() != 32 { return self.fail_auth(); }
        let mut stored = [0u8; 32];
        stored.copy_from_slice(hash);
        self.handshake_hash = Some(stored);
        self.response_produced = true;
        out.truncate(written);
        Ok(PyBytes::new(py, &out).unbind())
    }

    fn handshake_complete(&self) -> bool {
        !self.closed && self.response_produced && self.handshake_hash.is_some()
    }

    fn handshake_hash(&mut self, py: Python<'_>) -> PyResult<Py<PyBytes>> {
        self.require_open()?;
        match self.handshake_hash {
            Some(value) => Ok(PyBytes::new(py, &value).unbind()),
            None => self.fail_state(),
        }
    }

    fn enter_transport(&mut self) -> PyResult<()> {
        self.require_open()?;
        if self.transport.is_some() || !self.response_produced || self.handshake_hash.is_none() {
            return self.fail_state();
        }
        let handshake = match self.handshake.take() {
            Some(value) => value,
            None => return self.fail_state(),
        };
        match handshake.into_transport_mode() {
            Ok(transport) => { self.transport = Some(transport); Ok(()) }
            Err(_) => self.fail_auth(),
        }
    }

    fn encrypt_transport(&mut self, py: Python<'_>, plaintext: &[u8]) -> PyResult<Py<PyBytes>> {
        self.require_open()?;
        if plaintext.len() > ABSOLUTE_TRANSPORT_PLAINTEXT_MAX {
            self.close_internal();
            return Err(input_error());
        }
        let transport = match self.transport.as_mut() {
            Some(value) => value,
            None => return self.fail_state(),
        };
        let mut out = vec![0u8; plaintext.len() + TAG_LEN];
        let written = match transport.write_message(plaintext, &mut out) {
            Ok(value) if value <= out.len() => value,
            _ => return self.fail_auth(),
        };
        out.truncate(written);
        Ok(PyBytes::new(py, &out).unbind())
    }

    fn decrypt_transport(&mut self, py: Python<'_>, frame: &[u8]) -> PyResult<Py<PyBytes>> {
        self.require_open()?;
        if frame.is_empty() || frame.len() > ABSOLUTE_TRANSPORT_PLAINTEXT_MAX + TAG_LEN {
            self.close_internal();
            return Err(input_error());
        }
        let transport = match self.transport.as_mut() {
            Some(value) => value,
            None => return self.fail_state(),
        };
        let mut out = vec![0u8; frame.len()];
        let written = match transport.read_message(frame, &mut out) {
            Ok(value) if value <= out.len() => value,
            _ => return self.fail_auth(),
        };
        out.truncate(written);
        Ok(PyBytes::new(py, &out).unbind())
    }

    fn close(&mut self) { self.close_internal(); }
    fn closed(&self) -> bool { self.closed }
}

impl Drop for NoiseResponder {
    fn drop(&mut self) { self.close_internal(); }
}

#[pymodule]
fn ofca_native_snow(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<NoiseResponder>()?;
    module.add("NoiseError", py.get_type::<NoiseError>())?;
    module.add("NoiseInputError", py.get_type::<NoiseInputError>())?;
    module.add("NoiseStateError", py.get_type::<NoiseStateError>())?;
    module.add("NoiseAuthenticationError", py.get_type::<NoiseAuthenticationError>())?;
    module.add("NoiseClosedError", py.get_type::<NoiseClosedError>())?;
    module.add("SUITE", SUITE)?;
    Ok(())
}
