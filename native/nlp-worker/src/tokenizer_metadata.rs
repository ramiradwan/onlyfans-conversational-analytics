//! Inspect tokenizer-wide truncation without loading its vocabulary.
use crate::{context::BoundFile, entity::Control};
use anyhow::{Result, ensure};
use serde::{
    Deserialize, Deserializer,
    de::{IgnoredAny, MapAccess, Visitor},
};
use std::{
    collections::BTreeSet,
    fs::File,
    io::{BufReader, Read},
};

const MAX_BYTES: u64 = 64 * 1024 * 1024;
struct Metadata(bool);
impl<'de> Deserialize<'de> for Metadata {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        struct Root;
        impl<'de> Visitor<'de> for Root {
            type Value = Metadata;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("tokenizer metadata")
            }
            fn visit_map<A: MapAccess<'de>>(
                self,
                mut map: A,
            ) -> std::result::Result<Metadata, A::Error> {
                let mut keys = BTreeSet::new();
                let mut truncation = false;
                while let Some(key) = map.next_key::<String>()? {
                    if key.len() > 256 || keys.len() >= 64 || !keys.insert(key.clone()) {
                        return Err(serde::de::Error::custom("invalid tokenizer metadata"));
                    }
                    if key == "truncation" {
                        truncation = map.next_value::<Option<IgnoredAny>>()?.is_some();
                    } else {
                        let _ = map.next_value::<IgnoredAny>()?;
                    }
                }
                Ok(Metadata(truncation))
            }
        }
        deserializer.deserialize_map(Root)
    }
}
struct Checked<'a, R> {
    inner: R,
    read: u64,
    control: &'a Control,
}
impl<R: Read> Read for Checked<'_, R> {
    fn read(&mut self, bytes: &mut [u8]) -> std::io::Result<usize> {
        self.control
            .check()
            .map_err(|_| std::io::Error::other("cancelled"))?;
        let n = self.inner.read(bytes)?;
        self.read = self
            .read
            .checked_add(n as u64)
            .ok_or_else(|| std::io::Error::other("resource_limit"))?;
        if self.read > MAX_BYTES {
            return Err(std::io::Error::other("resource_limit"));
        }
        Ok(n)
    }
}
fn inspect(reader: impl Read, control: &Control) -> Result<()> {
    control.check()?;
    let checked = Checked {
        inner: reader,
        read: 0,
        control,
    };
    let mut deserializer =
        serde_json::Deserializer::from_reader(BufReader::with_capacity(4096, checked));
    let result = Metadata::deserialize(&mut deserializer).and_then(|value| {
        deserializer.end()?;
        Ok(value)
    });
    control.check()?;
    let metadata = result.map_err(|_| anyhow::anyhow!("artifact_mismatch"))?;
    ensure!(!metadata.0, "incompatible");
    Ok(())
}
pub fn check(file: &BoundFile, control: &Control) -> Result<()> {
    control.check()?;
    ensure!(
        file.artifact.size_bytes > 0 && file.artifact.size_bytes <= MAX_BYTES,
        "resource_limit"
    );
    let input = File::open(&file.path)?;
    ensure!(
        input.metadata()?.len() == file.artifact.size_bytes,
        "artifact_mismatch"
    );
    inspect(input, control)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        sync::{Arc, atomic::AtomicBool},
        time::{Duration, Instant},
    };
    fn control() -> Control {
        Control {
            cancelled: Arc::new(AtomicBool::new(false)),
            deadline: Instant::now() + Duration::from_secs(5),
        }
    }
    #[test]
    fn null_and_absent_truncation_preserve_admission() {
        for bytes in [
            br#"{}"#.as_slice(),
            br#"{"truncation":null}"#,
            br#"{"model":{"vocab":[["invented",1],["word",2]]},"padding":null,"truncation":null}"#,
        ] {
            assert!(inspect(bytes, &control()).is_ok());
        }
    }
    #[test]
    fn nonnull_duplicate_malformed_and_trailing_metadata_are_refused() {
        for bytes in [
            br#"{"truncation":{"max_length":1}}"#.as_slice(),
            br#"{"truncation":false}"#,
            br#"{"truncation":0}"#,
            br#"{"truncation":[],"truncation":null}"#,
            br#"{"model":{},"model":{}}"#,
            br#"[]"#,
            br#"{"model":{"vocab":[}}"#,
            br#"{} {}"#,
        ] {
            assert!(inspect(bytes, &control()).is_err());
        }
    }
    #[test]
    fn cancellation_and_deadline_are_checked_while_streaming() {
        struct Cancel<'a> {
            control: &'a Control,
        }
        impl Read for Cancel<'_> {
            fn read(&mut self, bytes: &mut [u8]) -> std::io::Result<usize> {
                self.control
                    .cancelled
                    .store(true, std::sync::atomic::Ordering::SeqCst);
                bytes[0] = b'{';
                Ok(1)
            }
        }
        let c = control();
        assert_eq!(
            inspect(Cancel { control: &c }, &c).unwrap_err().to_string(),
            "cancelled"
        );
        let mut c = control();
        c.deadline = Instant::now() - Duration::from_secs(1);
        assert_eq!(
            inspect(br#"{}"#.as_slice(), &c).unwrap_err().to_string(),
            "deadline"
        );
    }
    #[test]
    fn metadata_root_and_stream_byte_bounds_are_finite() {
        let bytes = format!("{{\"{}\":null}}", "x".repeat(257));
        assert!(inspect(bytes.as_bytes(), &control()).is_err());
        let c = control();
        let mut reader = Checked {
            inner: std::io::repeat(b' '),
            read: MAX_BYTES,
            control: &c,
        };
        assert!(reader.read(&mut [0; 1]).is_err());
    }
}
