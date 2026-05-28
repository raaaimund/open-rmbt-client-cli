mod control;
mod connection;
mod tests;

use anyhow::Result;
use clap::{Arg, ArgAction, Command};
use connection::Protocol;
use std::sync::{Arc, Barrier};
use std::thread;

const MAX_THREADS: u32 = 20;

fn main() -> Result<()> {
    // Install the ring TLS crypto provider before any TLS operations.
    let _ = rustls::crypto::ring::default_provider().install_default();

    let matches = Command::new("rmbt-client")
        .about("RMBT network measurement client")
        // Disable built-in -h/--help so -h can mean --host.
        .disable_help_flag(true)
        .disable_version_flag(true)
        .arg(
            Arg::new("help")
                .long("help")
                .action(ArgAction::Help)
                .help("Print this help"),
        )
        .arg(
            Arg::new("host")
                .short('h')
                .long("host")
                .value_name("URL")
                .required(true)
                .help("Control server base URL (e.g. https://measure.example.com)"),
        )
        .arg(
            Arg::new("port")
                .short('p')
                .long("port")
                .value_name("PORT")
                .help("Override test server port")
                .value_parser(clap::value_parser!(u16)),
        )
        .arg(
            Arg::new("uuid")
                .short('u')
                .long("uuid")
                .value_name("UUID")
                .help("Client UUID (leave empty on first run)"),
        )
        .arg(
            Arg::new("threads")
                .short('t')
                .long("threads")
                .value_name("N")
                .help("Parallel test threads (default: from control server)")
                .value_parser(clap::value_parser!(u32)),
        )
        .arg(
            Arg::new("duration")
                .short('d')
                .long("duration")
                .value_name("SECS")
                .help("Test duration in seconds (default: from control server)")
                .value_parser(clap::value_parser!(u32)),
        )
        .arg(
            Arg::new("ws")
                .long("ws")
                .action(ArgAction::SetTrue)
                .conflicts_with("http")
                .help("Use WebSocket (RMBTws) framing instead of plain HTTP upgrade"),
        )
        .arg(
            Arg::new("http")
                .long("http")
                .action(ArgAction::SetTrue)
                .conflicts_with("ws")
                .help("Use plain HTTP upgrade (RMBThttp) — overrides auto-detection"),
        )
        .arg(
            Arg::new("no-tls-verify")
                .long("no-tls-verify")
                .action(ArgAction::SetTrue)
                .help("Skip TLS certificate verification for test server (insecure)"),
        )
        .arg(
            Arg::new("debug")
                .long("debug")
                .action(ArgAction::SetTrue)
                .help("Print raw control server request and response JSON"),
        )
        .get_matches();

    let host          = matches.get_one::<String>("host").unwrap().as_str();
    let port_ovr      = matches.get_one::<u16>("port").copied();
    let uuid          = matches.get_one::<String>("uuid").map(|s| s.as_str());
    let threads_ovr   = matches.get_one::<u32>("threads").copied();
    let dur_ovr       = matches.get_one::<u32>("duration").copied();
    let force_ws      = matches.get_flag("ws");
    let force_http    = matches.get_flag("http");
    let no_tls_verify = matches.get_flag("no-tls-verify");
    let debug         = matches.get_flag("debug");

    // ── Step 1: request test parameters from the control server ──────────────
    println!("Contacting control server: {host}");
    let params = control::request_test(host, uuid, force_ws, debug)?;

    let preview_token = &params.token[..params.token.len().min(40)];
    println!("Token:    {preview_token}…");
    println!(
        "Server:   {}:{} ({})",
        params.server_addr,
        params.server_port,
        if params.encryption { "TLS" } else { "plain TCP" }
    );
    println!(
        "Threads:  {}  Duration: {}s  Pings: {}",
        params.num_threads, params.duration, params.num_pings
    );

    // Choose protocol: explicit flag > auto-detect from server_type.
    let protocol = if force_ws {
        Protocol::Ws
    } else if force_http {
        Protocol::Http
    } else if params.server_type == "RMBTws" {
        Protocol::Ws
    } else {
        Protocol::Http
    };

    println!(
        "Protocol: {}  (server_type: {})",
        match protocol { Protocol::Ws => "RMBTws", Protocol::Http => "RMBThttp" },
        if params.server_type.is_empty() { "unset" } else { &params.server_type },
    );

    if params.wait > 0 {
        println!("Waiting {}s before test…", params.wait);
        std::thread::sleep(std::time::Duration::from_secs(params.wait as u64));
    }

    let port        = port_ovr.unwrap_or(params.server_port);
    let duration    = dur_ovr.unwrap_or(params.duration);
    let num_threads = threads_ovr.unwrap_or(params.num_threads).max(1).min(MAX_THREADS) as usize;

    // ── Step 2: run the measurement ───────────────────────────────────────────
    println!("\nRunning test ({num_threads} thread(s), {duration}s per phase)…");

    let addr   = Arc::new(params.server_addr.clone());
    let token  = Arc::new(params.token.clone());

    // Ping: single thread only.
    let ping_rtts = {
        let mut conn = connection::RmbtConn::connect(
            &addr, port, params.encryption, no_tls_verify, protocol,
        )?;
        conn.greeting(&token)?;
        let rtts = tests::run_ping(&mut conn, params.num_pings)?;
        conn.quit()?;
        rtts
    };

    // Download: N threads in parallel, all start together.
    let dl_results = run_phase(
        num_threads, &addr, port, params.encryption, no_tls_verify, protocol, &token,
        move |conn| tests::run_download(conn, duration),
    )?;

    // Upload: same.
    let ul_results = run_phase(
        num_threads, &addr, port, params.encryption, no_tls_verify, protocol, &token,
        move |conn| tests::run_upload(conn, duration),
    )?;

    // ── Step 3: aggregate and print results ───────────────────────────────────
    let dl_bytes: u64 = dl_results.iter().map(|r| r.bytes).sum();
    let dl_ns           = dl_results.iter().map(|r| r.elapsed_ns).max().unwrap_or(1);
    let ul_bytes: u64 = ul_results.iter().map(|r| r.bytes).sum();
    let ul_ns           = ul_results.iter().map(|r| r.elapsed_ns).max().unwrap_or(1);

    let dl_mbps = dl_bytes as f64 * 8.0 / (dl_ns as f64 / 1e9) / 1_000_000.0;
    let ul_mbps = ul_bytes as f64 * 8.0 / (ul_ns as f64 / 1e9) / 1_000_000.0;
    let ping_min_ms    = ping_rtts.iter().copied().min().unwrap_or(0) as f64 / 1_000_000.0;
    let ping_median_ms = median_u64(&ping_rtts) as f64 / 1_000_000.0;

    println!("\n=== Results ===");
    println!("Ping (min):     {:7.2} ms", ping_min_ms);
    println!("Ping (median):  {:7.2} ms", ping_median_ms);
    println!("Download:       {:7.2} Mbit/s  ({} bytes in {:.2}s)",
             dl_mbps, dl_bytes, dl_ns as f64 / 1e9);
    println!("Upload:         {:7.2} Mbit/s  ({} bytes in {:.2}s)",
             ul_mbps, ul_bytes, ul_ns as f64 / 1e9);

    Ok(())
}

/// Spawn `n` threads, each connecting independently, hitting a barrier before
/// the test body, then calling `f` on the connection.  Returns all results.
fn run_phase<F>(
    n:             usize,
    addr:          &Arc<String>,
    port:          u16,
    use_tls:       bool,
    no_tls_verify: bool,
    protocol:      Protocol,
    token:         &Arc<String>,
    f:             F,
) -> Result<Vec<tests::TransferResult>>
where
    F: Fn(&mut connection::RmbtConn) -> Result<tests::TransferResult> + Send + Sync + 'static,
{
    let f   = Arc::new(f);
    let bar = Arc::new(Barrier::new(n));
    let mut handles = Vec::with_capacity(n);

    for _ in 0..n {
        let addr2  = addr.clone();
        let tok2   = token.clone();
        let bar2   = bar.clone();
        let f2     = f.clone();

        handles.push(thread::spawn(move || -> Result<tests::TransferResult> {
            let mut conn = connection::RmbtConn::connect(
                &addr2, port, use_tls, no_tls_verify, protocol,
            )?;
            conn.greeting(&tok2)?;
            bar2.wait();
            let r = f2(&mut conn)?;
            conn.quit()?;
            Ok(r)
        }));
    }

    handles
        .into_iter()
        .map(|h| h.join().expect("thread panicked"))
        .collect()
}

fn median_u64(v: &[u64]) -> u64 {
    if v.is_empty() {
        return 0;
    }
    let mut s = v.to_vec();
    s.sort_unstable();
    s[s.len() / 2]
}
