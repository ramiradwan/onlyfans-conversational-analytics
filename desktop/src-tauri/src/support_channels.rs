pub const COMPILED_CHANNELS: &str =
    include_str!(concat!(env!("OUT_DIR"), "/support-channels.json"));

pub fn initialization_script(channels: &str) -> String {
    if channels == "{}" {
        return String::new();
    }
    format!("({})({channels});", include_str!("../../startup-help.js"))
}

pub fn dialog_content(message: &str, projection: &str) -> String {
    let channels: serde_json::Value = serde_json::from_str(projection).expect("compiled support");
    let mut rows = [
        ("whatsapp", "WhatsApp"),
        ("telegram", "Telegram"),
        ("tiktok", "TikTok"),
    ]
    .into_iter()
    .filter_map(|(key, label)| {
        channels[key]
            .as_str()
            .map(|address| format!("{label} {address}"))
    })
    .peekable();
    if rows.peek().is_none() {
        return message.to_owned();
    }
    let mut content = format!("{message}\n\nGet help");
    for row in rows {
        content.push('\n');
        content.push_str(&row);
    }
    content
}
