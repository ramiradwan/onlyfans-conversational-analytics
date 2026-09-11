use snow::{Builder, HandshakeState, TransportState};
use wasm_bindgen::prelude::*;

const PROFILE: &str = "Noise_KK_25519_ChaChaPoly_SHA256";
const MAX_FRAME: usize = 4096;
const TAG_LEN: usize = 16;

// Return private || public so the caller can immediately wrap the private half
// with its non-exportable IndexedDB wrapping key and clear the temporary bytes.
#[wasm_bindgen]
pub fn generate_static_keypair() -> Result<Vec<u8>, JsValue> {
    let params = PROFILE.parse().map_err(|_| failure("unsupported_suite"))?;
    let mut pair = Builder::new(params).generate_keypair()
        .map_err(|_| failure("key_generation_failed"))?;
    let mut bytes = Vec::with_capacity(64);
    bytes.extend_from_slice(&pair.private);
    bytes.extend_from_slice(&pair.public);
    pair.private.fill(0);
    Ok(bytes)
}

fn failure(code: &'static str) -> JsValue {
    JsValue::from_str(code)
}

#[wasm_bindgen]
pub struct SnowSession {
    handshake: Option<HandshakeState>,
    transport: Option<TransportState>,
    closed: bool,
}

#[wasm_bindgen]
impl SnowSession {
    #[wasm_bindgen(constructor)]
    pub fn new(
        initiator: bool,
        local_private_key: &[u8],
        remote_public_key: &[u8],
        prologue: &[u8],
    ) -> Result<SnowSession, JsValue> {
        if local_private_key.len() != 32 || remote_public_key.len() != 32 || prologue.is_empty() || prologue.len() > 512 {
            return Err(failure("invalid_session_input"));
        }
        let params = PROFILE.parse().map_err(|_| failure("unsupported_suite"))?;
        let builder = Builder::new(params)
            .local_private_key(local_private_key).map_err(|_| failure("invalid_session_input"))?
            .remote_public_key(remote_public_key).map_err(|_| failure("invalid_session_input"))?
            .prologue(prologue).map_err(|_| failure("invalid_session_input"))?;
        let handshake = if initiator {
            builder.build_initiator()
        } else {
            builder.build_responder()
        }.map_err(|_| failure("handshake_init_failed"))?;
        Ok(Self { handshake: Some(handshake), transport: None, closed: false })
    }

    pub fn profile(&self) -> String { PROFILE.to_string() }

    pub fn write_handshake(&mut self) -> Result<Vec<u8>, JsValue> {
        if self.closed || self.transport.is_some() { return Err(failure("session_closed")); }
        let handshake = self.handshake.as_mut().ok_or_else(|| failure("session_closed"))?;
        let mut out = vec![0u8; MAX_FRAME];
        match handshake.write_message(&[], &mut out) {
            Ok(n) if n <= MAX_FRAME => { out.truncate(n); Ok(out) }
            _ => { self.close(); Err(failure("handshake_failed")) }
        }
    }

    pub fn read_handshake(&mut self, frame: &[u8]) -> Result<(), JsValue> {
        if self.closed || self.transport.is_some() { return Err(failure("session_closed")); }
        if frame.is_empty() || frame.len() > MAX_FRAME { self.close(); return Err(failure("invalid_frame")); }
        let handshake = self.handshake.as_mut().ok_or_else(|| failure("session_closed"))?;
        let mut payload = vec![0u8; MAX_FRAME];
        match handshake.read_message(frame, &mut payload) {
            Ok(0) => Ok(()),
            Ok(_) => { self.close(); Err(failure("early_data_refused")) }
            Err(_) => { self.close(); Err(failure("handshake_failed")) }
        }
    }

    pub fn handshake_finished(&self) -> bool {
        !self.closed && self.handshake.as_ref().is_some_and(HandshakeState::is_handshake_finished)
    }

    pub fn enter_transport(&mut self) -> Result<(), JsValue> {
        if self.closed || self.transport.is_some() { return Err(failure("session_closed")); }
        if !self.handshake_finished() { return Err(failure("handshake_incomplete")); }
        let handshake = self.handshake.take().ok_or_else(|| failure("session_closed"))?;
        match handshake.into_transport_mode() {
            Ok(transport) => { self.transport = Some(transport); Ok(()) }
            Err(_) => { self.close(); Err(failure("handshake_failed")) }
        }
    }

    pub fn encrypt_transport(&mut self, plaintext: &[u8]) -> Result<Vec<u8>, JsValue> {
        if self.closed || plaintext.len() > MAX_FRAME - TAG_LEN { return Err(failure("payload_too_large")); }
        let transport = self.transport.as_mut().ok_or_else(|| failure("session_not_ready"))?;
        let mut out = vec![0u8; MAX_FRAME];
        match transport.write_message(plaintext, &mut out) {
            Ok(n) if n <= MAX_FRAME => { out.truncate(n); Ok(out) }
            _ => { self.close(); Err(failure("encryption_failed")) }
        }
    }

    pub fn decrypt_transport(&mut self, frame: &[u8]) -> Result<Vec<u8>, JsValue> {
        if self.closed { return Err(failure("session_closed")); }
        if frame.is_empty() || frame.len() > MAX_FRAME { self.close(); return Err(failure("invalid_frame")); }
        let result = {
            let transport = self.transport.as_mut().ok_or_else(|| failure("session_not_ready"))?;
            let mut out = vec![0u8; MAX_FRAME];
            transport.read_message(frame, &mut out).map(|n| { out.truncate(n); out })
        };
        match result {
            Ok(out) => Ok(out),
            Err(_) => { self.close(); Err(failure("authentication_failed")) }
        }
    }

    pub fn close(&mut self) {
        self.closed = true;
        self.handshake = None;
        self.transport = None;
    }
}
