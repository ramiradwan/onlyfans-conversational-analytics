//! Bounded private frames and canonical descriptor identities.
use anyhow::{Result, ensure};
use serde::{
    Deserialize, Deserializer,
    de::{MapAccess, SeqAccess, Visitor},
};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::io::{Read, Write};
pub const MAX_FRAME: usize = 1_048_576;
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Envelope {
    pub channel: String,
    pub operation: String,
    pub payload: Value,
}
struct Unique(Value);
impl<'de> Deserialize<'de> for Unique {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = Unique;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("unique JSON")
            }
            fn visit_map<A: MapAccess<'de>>(self, mut a: A) -> Result<Unique, A::Error> {
                let mut m = Map::new();
                while let Some(k) = a.next_key::<String>()? {
                    if m.contains_key(&k) {
                        return Err(serde::de::Error::custom("duplicate member"));
                    }
                    m.insert(k, a.next_value::<Unique>()?.0);
                }
                Ok(Unique(Value::Object(m)))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut a: A) -> Result<Unique, A::Error> {
                let mut v = Vec::new();
                while let Some(x) = a.next_element::<Unique>()? {
                    v.push(x.0)
                }
                Ok(Unique(Value::Array(v)))
            }
            fn visit_str<E: serde::de::Error>(self, v: &str) -> Result<Unique, E> {
                Ok(Unique(Value::String(v.into())))
            }
            fn visit_string<E: serde::de::Error>(self, v: String) -> Result<Unique, E> {
                Ok(Unique(Value::String(v)))
            }
            fn visit_bool<E: serde::de::Error>(self, v: bool) -> Result<Unique, E> {
                Ok(Unique(Value::Bool(v)))
            }
            fn visit_i64<E: serde::de::Error>(self, v: i64) -> Result<Unique, E> {
                Ok(Unique(Value::Number(v.into())))
            }
            fn visit_u64<E: serde::de::Error>(self, v: u64) -> Result<Unique, E> {
                Ok(Unique(Value::Number(v.into())))
            }
            fn visit_f64<E: serde::de::Error>(self, v: f64) -> Result<Unique, E> {
                serde_json::Number::from_f64(v)
                    .map(|n| Unique(Value::Number(n)))
                    .ok_or_else(|| E::custom("nonfinite"))
            }
            fn visit_unit<E: serde::de::Error>(self) -> Result<Unique, E> {
                Ok(Unique(Value::Null))
            }
        }
        d.deserialize_any(V)
    }
}
pub fn parse(bytes: &[u8]) -> Result<Value> {
    ensure!(
        !bytes.is_empty() && bytes.len() <= MAX_FRAME,
        "invalid_frame"
    );
    Ok(serde_json::from_slice::<Unique>(bytes)
        .map_err(|_| anyhow::anyhow!("invalid_frame"))?
        .0)
}
pub fn envelope(bytes: &[u8]) -> Result<Envelope> {
    let v = parse(bytes)?;
    let e: Envelope = serde_json::from_value(v)?;
    ensure!(e.channel == "nlp-child.v1", "invalid_frame");
    ensure!(
        [
            "bootstrap",
            "handshake",
            "prepare",
            "analyze",
            "cancel",
            "unload",
            "close"
        ]
        .contains(&e.operation.as_str()),
        "unexpected_operation"
    );
    ensure!(e.payload.is_object(), "invalid_payload");
    Ok(e)
}
pub fn read_frame(reader: &mut impl Read) -> Result<Vec<u8>> {
    let mut h = [0; 4];
    reader.read_exact(&mut h)?;
    let n = u32::from_le_bytes(h) as usize;
    ensure!((1..=MAX_FRAME).contains(&n), "invalid_frame");
    let mut b = vec![0; n];
    reader.read_exact(&mut b)?;
    Ok(b)
}
pub fn write_frame(writer: &mut impl Write, operation: &str, payload: Value) -> Result<()> {
    let b = serde_json::to_vec(
        &serde_json::json!({"channel":"nlp-child.v1","operation":operation,"payload":payload}),
    )?;
    ensure!(!b.is_empty() && b.len() <= MAX_FRAME, "resource_limit");
    writer.write_all(&(b.len() as u32).to_le_bytes())?;
    writer.write_all(&b)?;
    writer.flush()?;
    Ok(())
}
pub fn object<'a>(v: &'a Value, keys: &[&str]) -> Result<&'a Map<String, Value>> {
    let m = v
        .as_object()
        .ok_or_else(|| anyhow::anyhow!("invalid_payload"))?;
    ensure!(
        m.len() == keys.len() && keys.iter().all(|k| m.contains_key(*k)),
        "invalid_payload"
    );
    Ok(m)
}
pub fn id(v: &Value) -> Result<&str> {
    let s = v
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("invalid_payload"))?;
    ensure!(
        !s.is_empty()
            && s.len() <= 160
            && s.bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"_.:-".contains(&c)),
        "invalid_payload"
    );
    Ok(s)
}
pub fn hex(v: &str, n: usize) -> bool {
    v.len() == n
        && v.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
pub fn hash(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}
pub fn canonical(v: &Value) -> Result<String> {
    fn string(s: &str, out: &mut String) {
        out.push('"');
        for c in s.chars() {
            match c {
                '"' => out.push_str("\\\""),
                '\\' => out.push_str("\\\\"),
                '\n' => out.push_str("\\n"),
                '\r' => out.push_str("\\r"),
                '\t' => out.push_str("\\t"),
                '\u{8}' => out.push_str("\\b"),
                '\u{c}' => out.push_str("\\f"),
                c if (' '..='~').contains(&c) => out.push(c),
                c => {
                    let mut units = [0; 2];
                    for u in c.encode_utf16(&mut units) {
                        out.push_str(&format!("\\u{u:04x}"))
                    }
                }
            }
        }
        out.push('"')
    }
    fn encode(v: &Value, out: &mut String, depth: usize) -> Result<()> {
        ensure!(depth <= 64, "resource_limit");
        match v {
            Value::Null => out.push_str("null"),
            Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Value::String(s) => string(s, out),
            Value::Number(n) => {
                ensure!(
                    n.as_u64().is_some_and(|n| n <= 9_007_199_254_740_991)
                        || n.as_i64()
                            .is_some_and(|n| n.unsigned_abs() <= 9_007_199_254_740_991),
                    "invalid_payload"
                );
                out.push_str(&n.to_string())
            }
            Value::Array(a) => {
                out.push('[');
                for (i, x) in a.iter().enumerate() {
                    if i > 0 {
                        out.push(',')
                    }
                    encode(x, out, depth + 1)?
                }
                out.push(']')
            }
            Value::Object(m) => {
                out.push('{');
                let mut keys = m.keys().collect::<Vec<_>>();
                keys.sort();
                for (i, k) in keys.iter().enumerate() {
                    if i > 0 {
                        out.push(',')
                    }
                    string(k, out);
                    out.push(':');
                    encode(&m[*k], out, depth + 1)?
                }
                out.push('}')
            }
        }
        Ok(())
    }
    let mut out = String::new();
    encode(v, &mut out, 0)?;
    Ok(out)
}
pub fn fail(reason: &str) -> Value {
    serde_json::json!({"reason":reason})
}
#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;
    #[test]
    fn duplicate_nested_refused() {
        assert!(parse(br#"{"a":{"b":1,"b":2}}"#).is_err());
    }
    #[test]
    fn frames_fail_before_large_allocation() {
        for n in [0, MAX_FRAME + 1] {
            assert!(read_frame(&mut Cursor::new((n as u32).to_le_bytes())).is_err())
        }
        assert!(read_frame(&mut Cursor::new([2, 0, 0, 0, b'{'])).is_err());
    }
    #[test]
    fn canonical_ascii_matches_descriptor_spelling() {
        assert_eq!(
            canonical(&serde_json::json!({"z":"😀é","a":[true,2]})).unwrap(),
            r#"{"a":[true,2],"z":"\ud83d\ude00\u00e9"}"#
        );
        assert!(canonical(&serde_json::json!(0.5)).is_err());
    }
    #[test]
    fn transport_roundtrip_is_closed() {
        let mut b = Vec::new();
        write_frame(&mut b, "close", serde_json::json!({"request_id":"one"})).unwrap();
        let frame = read_frame(&mut Cursor::new(b)).unwrap();
        assert_eq!(envelope(&frame).unwrap().operation, "close");
        assert!(
            envelope(br#"{"channel":"nlp-child.v1","operation":"close","payload":{},"x":0}"#)
                .is_err()
        );
    }
}
