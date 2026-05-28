use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};

/// Parameters returned by the control server for a single test session.
pub struct TestParams {
    pub token:       String,
    pub server_addr: String,
    pub server_port: u16,
    pub encryption:  bool,
    pub duration:    u32,
    pub num_threads: u32,
    pub num_pings:   u32,
    pub wait:        u32,
    pub server_type: String, // "RMBThttp" or "RMBTws"
}

// ─── Wire types ───────────────────────────────────────────────────────────────

#[derive(Serialize)]
struct TestRequest<'a> {
    #[serde(skip_serializing_if = "Option::is_none")]
    uuid:                Option<&'a str>,
    client:              &'a str,
    version:             &'a str,
    #[serde(rename = "type")]
    client_type:         &'a str,
    #[serde(rename = "softwareVersion")]
    software_version:    &'a str,
    #[serde(rename = "softwareRevision")]
    software_revision:   &'a str,
    language:            &'a str,
    timezone:            &'a str,
    time:                u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    capabilities:        Option<serde_json::Value>,
}

#[derive(Deserialize)]
struct TestResponse {
    test_token:             Option<String>,
    #[allow(dead_code)]
    test_uuid:              Option<String>,
    test_server_address:    Option<String>,
    test_server_port:       Option<serde_json::Value>, // int or string in the wild
    test_server_encryption: Option<bool>,
    test_server_type:       Option<String>,
    #[serde(default, deserialize_with = "de_opt_u32")]
    test_duration:          Option<u32>,
    #[serde(default, deserialize_with = "de_opt_u32")]
    test_numthreads:        Option<u32>,
    #[serde(default, deserialize_with = "de_opt_u32")]
    test_numpings:          Option<u32>,
    #[serde(default, deserialize_with = "de_opt_u32")]
    test_wait:              Option<u32>,
    #[serde(default)]
    error:                  Vec<String>,
}

/// Deserialize an `Option<u32>` that may arrive as a JSON number or a JSON
/// string (e.g. `"7"` instead of `7`).  Returns `None` for JSON null or an
/// unparseable string.
fn de_opt_u32<'de, D>(de: D) -> std::result::Result<Option<u32>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let v = <serde_json::Value as serde::Deserialize>::deserialize(de)?;
    match v {
        serde_json::Value::Number(n) => Ok(n.as_u64().map(|x| x as u32)),
        serde_json::Value::String(s) => Ok(s.parse().ok()),
        serde_json::Value::Null => Ok(None),
        other => Err(serde::de::Error::custom(
            format!("expected number or string for u32 field, got {other}"),
        )),
    }
}

// ─── Public API ───────────────────────────────────────────────────────────────

/// POST to `{host}/RMBTControlServer/testRequest` and return the test parameters.
///
/// `use_ws` controls whether the request identifies as `"RMBTws"` (WebSocket)
/// or `"RMBT"` (plain HTTP upgrade).  When `debug` is true the request JSON
/// and raw response are printed to stderr.
pub fn request_test(host: &str, uuid: Option<&str>, use_ws: bool, debug: bool) -> Result<TestParams> {
    let base = host.trim_end_matches('/');
    let url  = format!("{base}/RMBTControlServer/testRequest");

    let now_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64;

    let client_id = if use_ws { "RMBTws" } else { "RMBT" };

    let capabilities = if !use_ws {
        Some(serde_json::json!({ "RMBThttp": true }))
    } else {
        None
    };

    let body = serde_json::to_value(TestRequest {
        uuid,
        client:            client_id,
        version:           "0.9",
        client_type:       "DESKTOP",
        software_version:  "0.9",
        software_revision: "_v0.9.3",
        language:          "en",
        timezone:          "UTC",
        time:              now_ms,
        capabilities,
    })?;

    if debug {
        eprintln!("[debug] POST {url}");
        eprintln!("[debug] request body:\n{}", serde_json::to_string_pretty(&body)?);
    }

    let raw: String = match ureq::post(&url)
        .set("Content-Type", "application/json")
        .send_json(body)
    {
        Ok(r)  => r.into_string().context("failed to read control server response")?,
        Err(ureq::Error::Status(code, r)) => {
            let body = r.into_string().unwrap_or_default();
            if debug {
                eprintln!("[debug] HTTP {code} response:\n{body}");
            }
            bail!("control server returned HTTP {code}: {}", body.trim());
        }
        Err(e) => bail!("control server request failed: {e}"),
    };

    if debug {
        let pretty = serde_json::from_str::<serde_json::Value>(&raw)
            .map(|v| serde_json::to_string_pretty(&v).unwrap_or_else(|_| raw.clone()))
            .unwrap_or_else(|_| raw.clone());
        eprintln!("[debug] response body:\n{pretty}");
    }

    let resp: TestResponse = serde_json::from_str(&raw)
        .context("failed to parse control server JSON")?;

    if !resp.error.is_empty() {
        bail!("control server error(s): {}", resp.error.join("; "));
    }

    let server_port = match &resp.test_server_port {
        Some(serde_json::Value::Number(n)) => n.as_u64().unwrap_or(443) as u16,
        Some(serde_json::Value::String(s)) => s.parse().unwrap_or(443),
        _ => 443,
    };

    Ok(TestParams {
        token:       resp.test_token.context("missing test_token")?,
        server_addr: resp.test_server_address.context("missing test_server_address")?,
        server_port,
        encryption:  resp.test_server_encryption.unwrap_or(true),
        duration:    resp.test_duration.unwrap_or(10),
        num_threads: resp.test_numthreads.unwrap_or(4),
        num_pings:   resp.test_numpings.unwrap_or(10),
        wait:        resp.test_wait.unwrap_or(0),
        server_type: resp.test_server_type.unwrap_or_default(),
    })
}
