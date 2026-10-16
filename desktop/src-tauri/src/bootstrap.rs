//! Startup observations have bounded time and bytes, and grant no admission.
use serde::Deserialize;
use std::time::{Duration, Instant};

pub const MAX_FRAME_BYTES: usize = 512;
pub const MAX_FRAMES: u32 = 256;
pub const MAX_TOTAL_BYTES: usize = 65536;
const FAST_SECONDS: u64 = 30;
const ABSOLUTE_SECONDS: u64 = 900;
const SILENCE_SECONDS: u64 = 15;

/// The owned child may exit before its final status frame is read. This is
/// failure classification only; an exit code never grants listener admission.
pub fn startup_exit_error(code: u32) -> &'static str {
    if code == 78 {
        "child_recovery_required"
    } else {
        "child_start_failed"
    }
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "kebab-case")]
enum Phase {
    Starting,
    Checking,
    Checkpointing,
    Migrating,
    Validating,
    ReadyToCheckListener,
    RecoveryRequired,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "kebab-case")]
enum Reason {
    RetainedData,
    WriterConflict,
    Maintenance,
    Cancelled,
    Installation,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Frame {
    schema: String,
    seq: u32,
    phase: Phase,
    #[serde(deserialize_with = "required_reason")]
    reason: Option<Reason>,
}
fn required_reason<'de, D: serde::Deserializer<'de>>(
    decoder: D,
) -> Result<Option<Reason>, D::Error> {
    Option::<Reason>::deserialize(decoder)
}

pub struct Supervisor {
    started: Instant,
    phase_started: Instant,
    last_seen: Instant,
    phase: Phase,
    sequence: u32,
    total_bytes: usize,
    buffer: Vec<u8>,
}
impl Supervisor {
    pub fn new(now: Instant) -> Self {
        Self {
            started: now,
            phase_started: now,
            last_seen: now,
            phase: Phase::Starting,
            sequence: 0,
            total_bytes: 0,
            buffer: Vec::new(),
        }
    }
    pub fn receive(&mut self, bytes: &[u8], now: Instant) -> Result<(), &'static str> {
        self.check_deadline(now)?;
        self.total_bytes = self
            .total_bytes
            .checked_add(bytes.len())
            .ok_or("bootstrap_bytes_invalid")?;
        if self.total_bytes > MAX_TOTAL_BYTES {
            return Err("bootstrap_bytes_invalid");
        }
        for byte in bytes {
            self.buffer.push(*byte);
            if self.buffer.len() > MAX_FRAME_BYTES {
                return Err("bootstrap_frame_invalid");
            }
            if *byte != b'\n' {
                continue;
            }
            let frame: Frame = serde_json::from_slice(&self.buffer[..self.buffer.len() - 1])
                .map_err(|_| "bootstrap_frame_invalid")?;
            self.buffer.clear();
            if frame.schema != "ofca-bootstrap-status/v1"
                || self.sequence == MAX_FRAMES
                || frame.seq != self.sequence + 1
                || frame.phase < self.phase
                || self.ready()
                || self.phase == Phase::RecoveryRequired
                || (frame.phase == Phase::RecoveryRequired) != frame.reason.is_some()
                || (self.sequence == 0 && frame.phase != Phase::Starting)
            {
                return Err("bootstrap_transition_invalid");
            }
            self.sequence = frame.seq;
            self.last_seen = now;
            if frame.phase != self.phase {
                self.phase_started = now;
            }
            self.phase = frame.phase;
            if self.phase == Phase::RecoveryRequired {
                return Err("child_recovery_required");
            }
        }
        Ok(())
    }
    pub fn ready(&self) -> bool {
        self.phase == Phase::ReadyToCheckListener
    }
    pub fn shutdown_receipt(&self) -> Result<ShutdownReceipt, &'static str> {
        if !self.ready() || !self.buffer.is_empty() {
            return Err("runtime_shutdown_not_ready");
        }
        Ok(ShutdownReceipt {
            after_sequence: self.sequence,
            bytes: Vec::new(),
            drained: None,
            invalid: false,
        })
    }
    pub fn check_deadline(&self, now: Instant) -> Result<(), &'static str> {
        let phase_seconds = match self.phase {
            Phase::Starting | Phase::ReadyToCheckListener | Phase::RecoveryRequired => FAST_SECONDS,
            Phase::Checking | Phase::Checkpointing | Phase::Validating => 300,
            Phase::Migrating => 600,
        };
        // Heartbeats never move a phase or absolute deadline. The initial fast
        // start and the final listener wait remain 30 seconds each. Inspection
        // itself has an explicit budget, even without pending migrations.
        if now.duration_since(self.started) >= Duration::from_secs(ABSOLUTE_SECONDS)
            || now.duration_since(self.phase_started) >= Duration::from_secs(phase_seconds)
            || (!self.ready()
                && self.sequence > 0
                && now.duration_since(self.last_seen) >= Duration::from_secs(SILENCE_SECONDS))
        {
            Err("child_start_timeout")
        } else {
            Ok(())
        }
    }
}

/// A single bounded child-owned tail, bound to the startup already admitted.
pub struct ShutdownReceipt {
    after_sequence: u32,
    bytes: Vec<u8>,
    drained: Option<bool>,
    invalid: bool,
}
impl ShutdownReceipt {
    pub fn receive(&mut self, bytes: &[u8]) -> Result<(), &'static str> {
        let result = self.receive_inner(bytes);
        self.invalid |= result.is_err();
        result
    }
    fn receive_inner(&mut self, bytes: &[u8]) -> Result<(), &'static str> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Receipt {
            schema: String,
            after_seq: u32,
            drained: bool,
        }
        if self.invalid {
            return Err("runtime_shutdown_frame_invalid");
        }
        if bytes.is_empty() {
            return Ok(());
        }
        if self.drained.is_some() || self.bytes.len().saturating_add(bytes.len()) > MAX_FRAME_BYTES
        {
            return Err("runtime_shutdown_frame_invalid");
        }
        self.bytes.extend_from_slice(bytes);
        if let Some(end) = self.bytes.iter().position(|byte| *byte == b'\n') {
            if end != self.bytes.len() - 1 {
                return Err("runtime_shutdown_frame_invalid");
            }
            let receipt: Receipt = serde_json::from_slice(&self.bytes[..end])
                .map_err(|_| "runtime_shutdown_frame_invalid")?;
            if receipt.schema != "ofca-runtime-shutdown/v1"
                || receipt.after_seq != self.after_sequence
            {
                return Err("runtime_shutdown_frame_invalid");
            }
            self.drained = Some(receipt.drained);
            self.bytes.clear();
        }
        Ok(())
    }
    pub fn confirmed(&self) -> bool {
        self.require_drained().is_ok()
    }
    pub fn require_drained(&self) -> Result<(), &'static str> {
        if self.invalid {
            return Err("runtime_shutdown_frame_invalid");
        }
        if !self.bytes.is_empty() || self.drained.is_none() {
            return Err("servicing_child_drain_unconfirmed");
        }
        if self.drained == Some(false) {
            return Err("servicing_child_owners_undrained");
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn shutdown_receipt_requires_one_closed_tail_from_the_admitted_startup() {
        let now = Instant::now();
        let mut startup = Supervisor::new(now);
        assert!(startup.shutdown_receipt().is_err());
        startup
            .receive(
                include_bytes!("../../../shared/bootstrap-status/happy.ndjson"),
                now,
            )
            .unwrap();
        let mut receipt = startup.shutdown_receipt().unwrap();
        assert!(!receipt.confirmed());
        assert_eq!(
            receipt.require_drained(),
            Err("servicing_child_drain_unconfirmed")
        );
        for byte in include_bytes!("../../../shared/bootstrap-status/shutdown.ndjson") {
            receipt.receive(&[*byte]).unwrap();
        }
        assert!(receipt.confirmed());
        assert_eq!(receipt.require_drained(), Ok(()));
        assert!(receipt.receive(b"\n").is_err());
        assert!(!receipt.confirmed());
        assert_eq!(
            receipt.require_drained(),
            Err("runtime_shutdown_frame_invalid")
        );
        assert!(receipt.receive(b"").is_err());
        for invalid in [
            "{\"schema\":\"ofca-runtime-shutdown/v1\",\"after_seq\":5,\"drained\":true}\n",
            "{\"schema\":\"ofca-runtime-shutdown/v1\",\"after_seq\":6,\"drained\":true,\"grant\":true}\n",
            "{\"schema\":\"ofca-runtime-shutdown/v1\",\"after_seq\":6,\"drained\":true,\"drained\":false}\n",
            "{\"schema\":\"ofca-runtime-shutdown/v1\",\"after_seq\":6,\"drained\":true}\n \n",
        ] {
            assert!(startup.shutdown_receipt().unwrap().receive(invalid.as_bytes()).is_err());
        }
        let mut refused = startup.shutdown_receipt().unwrap();
        refused
            .receive(
                b"{\"schema\":\"ofca-runtime-shutdown/v1\",\"after_seq\":6,\"drained\":false}\n",
            )
            .unwrap();
        assert!(!refused.confirmed());
        assert_eq!(
            refused.require_drained(),
            Err("servicing_child_owners_undrained")
        );
        assert!(startup
            .shutdown_receipt()
            .unwrap()
            .receive(&[b'x'; MAX_FRAME_BYTES + 1])
            .is_err());
    }
    #[test]
    fn owning_python_trace_has_no_listener_admission_authority() {
        let now = Instant::now();
        let mut status = Supervisor::new(now);
        status
            .receive(
                include_bytes!("../../../shared/bootstrap-status/happy.ndjson"),
                now,
            )
            .unwrap();
        assert!(status.ready());
        // This object only reports readiness for the separate listener checks.
        assert_eq!(status.sequence, 6);
    }
    fn frame(seq: u32, phase: &str) -> Vec<u8> {
        format!("{{\"schema\":\"ofca-bootstrap-status/v1\",\"seq\":{seq},\"phase\":\"{phase}\",\"reason\":null}}\n").into_bytes()
    }
    #[test]
    fn inspection_outlives_fast_start_without_admitting_listener() {
        let start = Instant::now();
        let mut status = Supervisor::new(start);
        status.receive(&frame(1, "starting"), start).unwrap();
        status.receive(&frame(2, "checking"), start).unwrap();
        for seq in 3..=10 {
            status
                .receive(
                    &frame(seq, "checking"),
                    start + Duration::from_secs((seq - 2) as u64 * 5),
                )
                .unwrap();
        }
        assert!(!status.ready());
        status
            .receive(
                &frame(11, "ready-to-check-listener"),
                start + Duration::from_secs(41),
            )
            .unwrap();
        assert!(status.ready());
        assert!(status
            .check_deadline(start + Duration::from_secs(70))
            .is_ok());
        assert!(status
            .check_deadline(start + Duration::from_secs(71))
            .is_err());
    }
    #[test]
    fn framing_is_incremental_and_refuses_duplicate_fields_and_unknown_authority() {
        let start = Instant::now();
        let mut status = Supervisor::new(start);
        for byte in frame(1, "starting") {
            status.receive(&[byte], start).unwrap();
        }
        assert!(status.receive(b"{\"schema\":\"ofca-bootstrap-status/v1\",\"seq\":2,\"seq\":2,\"phase\":\"checking\",\"reason\":null}\n",start).is_err());
        let mut status = Supervisor::new(start);
        assert!(status.receive(b"{\"schema\":\"ofca-bootstrap-status/v1\",\"seq\":1,\"phase\":\"starting\",\"reason\":null,\"authorized\":true}\n",start).is_err());
    }
    #[test]
    fn heartbeat_cannot_extend_phase_or_absolute_budget() {
        let start = Instant::now();
        let mut status = Supervisor::new(start);
        status.receive(&frame(1, "starting"), start).unwrap();
        status.receive(&frame(2, "checking"), start).unwrap();
        for seq in 3..62 {
            status
                .receive(
                    &frame(seq, "checking"),
                    start + Duration::from_secs((seq - 2) as u64 * 5),
                )
                .unwrap();
        }
        assert!(status
            .receive(&frame(62, "checking"), start + Duration::from_secs(300))
            .is_err());
    }
    #[test]
    fn silent_hung_child_and_phase_regression_are_refused() {
        let start = Instant::now();
        let mut status = Supervisor::new(start);
        status.receive(&frame(1, "starting"), start).unwrap();
        status.receive(&frame(2, "checking"), start).unwrap();
        assert!(status
            .check_deadline(start + Duration::from_secs(15))
            .is_err());
        assert!(status
            .receive(&frame(3, "starting"), start + Duration::from_secs(1))
            .is_err());
    }

    #[test]
    fn refusal_exit_and_status_frame_share_a_closed_recovery_outcome() {
        let start = Instant::now();
        let mut status = Supervisor::new(start);
        status.receive(&frame(1, "starting"), start).unwrap();
        let result = status.receive(b"{\"schema\":\"ofca-bootstrap-status/v1\",\"seq\":2,\"phase\":\"recovery-required\",\"reason\":\"maintenance\"}\n", start);
        assert_eq!(result, Err(startup_exit_error(78)));
        assert!(!status.ready());
        for code in [0, 1, 75, u32::MAX] {
            assert_eq!(startup_exit_error(code), "child_start_failed");
        }
    }
}
