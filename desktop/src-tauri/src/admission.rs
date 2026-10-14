use crate::policy::ORIGIN;
use base64::Engine;
use std::time::Duration;

pub fn launcher_secret() -> Result<String, &'static str> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|_| "randomness_unavailable")?;
    Ok(base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes))
}

fn handoff_code(value: &str) -> bool {
    value.len() == 43
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

/// A browser return focuses an already admitted presentation without replacing
/// its browser task. The launcher credential never enters either renderer.
pub fn presentation_current(
    secret: &str,
    document: &url::Url,
    journey: Option<&str>,
) -> Result<bool, &'static str> {
    if !crate::policy::local_document(document)
        || document.path() != "/onboarding"
        || document.query().is_some()
    {
        return Ok(false);
    }
    let Some(generation) = document
        .fragment()
        .and_then(|value| value.strip_prefix("presentation="))
    else {
        return Ok(false);
    };
    if uuid::Uuid::parse_str(generation).is_err() {
        return Ok(false);
    }
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .proxy(None)
        .max_redirects(0)
        .timeout_global(Some(Duration::from_secs(5)))
        .build()
        .into();
    let mut response = agent
        .post("http://127.0.0.1:17871/api/v1/desktop/presentation/current")
        .header("Host", "bridge.localhost:17871")
        .header("Authorization", format!("Desktop {secret}"))
        .send_json(serde_json::json!({"generation": generation, "journey_id": journey}))
        .map_err(|_| "handoff_unavailable")?;
    let body: serde_json::Value = response
        .body_mut()
        .with_config()
        .limit(1024)
        .read_json()
        .map_err(|_| "handoff_rejected")?;
    if body.as_object().map(|value| value.len()) != Some(1) {
        return Err("handoff_rejected");
    }
    body["current"].as_bool().ok_or("handoff_rejected")
}

/// Fixed loopback transport, no proxy, redirects or cookie store.
pub fn entry(secret: &str, journey: Option<&str>) -> Result<url::Url, &'static str> {
    let code = request_entry(
        secret,
        "/api/v1/desktop/entry",
        serde_json::json!({"journey_id": journey}),
    )?;
    entry_url(&serde_json::json!({"entry_code": code}))
}

pub fn refresh(secret: &str, generation: &str) -> Result<String, &'static str> {
    request_entry(
        secret,
        "/api/v1/desktop/presentation/refresh",
        serde_json::json!({"generation": generation}),
    )
}

fn request_entry(
    secret: &str,
    path: &str,
    body: serde_json::Value,
) -> Result<String, &'static str> {
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .proxy(None)
        .max_redirects(0)
        .http_status_as_error(false)
        .timeout_global(Some(Duration::from_secs(5)))
        .build()
        .into();
    let mut response = agent
        .post(format!("http://127.0.0.1:17871{path}"))
        .header("Host", "bridge.localhost:17871")
        .header("Authorization", format!("Desktop {secret}"))
        .send_json(body)
        .map_err(|_| "handoff_unavailable")?;
    if response.headers().contains_key("set-cookie") {
        return Err("handoff_rejected");
    }
    let status = response.status().as_u16();
    let body: serde_json::Value = response
        .body_mut()
        .with_config()
        .limit(4096)
        .read_json()
        .map_err(|_| "handoff_rejected")?;
    if status != 200 {
        return Err("handoff_rejected");
    }
    entry_url(&body)?;
    Ok(body["entry_code"]
        .as_str()
        .ok_or("handoff_rejected")?
        .to_owned())
}

fn entry_url(body: &serde_json::Value) -> Result<url::Url, &'static str> {
    if body.as_object().map(|value| value.len()) != Some(1) {
        return Err("handoff_rejected");
    }
    let code = body["entry_code"]
        .as_str()
        .filter(|value| handoff_code(value))
        .ok_or("handoff_rejected")?;
    url::Url::parse(&format!("{ORIGIN}/onboarding/entry#code={code}"))
        .map_err(|_| "handoff_rejected")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn entry_is_a_fixed_fragment_landing_without_configuration_selection() {
        let code = "A".repeat(43);
        let url = entry_url(&serde_json::json!({"entry_code": code})).unwrap();
        assert_eq!(url.path(), "/onboarding/entry");
        assert_eq!(url.query(), None);
        assert_eq!(url.fragment(), Some(format!("code={code}").as_str()));
    }

    #[test]
    fn entry_cannot_choose_an_external_destination_or_accept_old_handoffs() {
        for body in [
            serde_json::json!({"entry_code": "A".repeat(43), "url": "https://example.com"}),
            serde_json::json!({"handoff_code": "A".repeat(43)}),
            serde_json::json!({"entry_code": "?code=invalid"}),
        ] {
            assert!(entry_url(&body).is_err());
        }
    }
}
