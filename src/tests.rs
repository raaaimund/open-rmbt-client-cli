use anyhow::{bail, Result};
use std::time::Instant;

use crate::connection::RmbtConn;

/// Chunk size requested from the server for download and upload phases.
/// Larger chunks reduce per-chunk syscall and framing overhead compared to
/// the server's default 4 KiB.
const PREFERRED_CHUNK_SIZE: usize = 256 * 1024; // 256 KiB

pub struct TransferResult {
    pub bytes:      u64,
    pub elapsed_ns: u64,
}

// ─── Ping ─────────────────────────────────────────────────────────────────────

/// Run `count` PING/PONG exchanges.  Returns client-side RTT for each ping in
/// nanoseconds (time from sending PING to receiving PONG).
pub fn run_ping(conn: &mut RmbtConn, count: u32) -> Result<Vec<u64>> {
    let mut rtts = Vec::with_capacity(count as usize);

    for _ in 0..count {
        // "ACCEPT GETCHUNKS GETTIME PUT PUTNORESULT PING QUIT"
        let accept = conn.read_accept()?;
        if !accept.contains("PING") {
            bail!("expected ACCEPT with PING, got: {accept}");
        }

        let t0 = Instant::now();
        conn.write_line("PING")?;

        let pong = conn.read_line()?;
        let rtt  = t0.elapsed().as_nanos() as u64;

        if pong != "PONG" {
            bail!("expected PONG, got: {pong}");
        }

        conn.write_line("OK")?;
        let time_line = conn.read_line()?; // "TIME <ns>"  (server-side half-RTT)
        let server_ns = parse_time_ns(&time_line)?;

        println!(
            "  ping  client={:.3}ms  server={:.3}ms",
            rtt as f64 / 1_000_000.0,
            server_ns as f64 / 1_000_000.0
        );
        rtts.push(rtt);
    }

    Ok(rtts)
}

// ─── Download (GETTIME) ───────────────────────────────────────────────────────

/// Issue a GETTIME command for `duration_secs` seconds.
///
/// Reads chunks of `conn.chunk_size` bytes until the terminal byte (0xFF) is
/// seen at the last position of a chunk, sends OK, then receives TIME.
/// Matching the reference client's lastByte tracking across partial reads.
pub fn run_download(conn: &mut RmbtConn, duration_secs: u32) -> Result<TransferResult> {
    let accept = conn.read_accept()?;
    if !accept.contains("GETTIME") {
        bail!("expected ACCEPT with GETTIME, got: {accept}");
    }

    conn.write_line(&format!("GETTIME {duration_secs} {PREFERRED_CHUNK_SIZE}"))?;

    let t0        = Instant::now();
    let mut total = 0u64;
    let mut buf   = vec![0u8; PREFERRED_CHUNK_SIZE];

    // The server sends exactly PREFERRED_CHUNK_SIZE bytes per chunk.
    // read_exact aligns perfectly; check the last byte for the 0xFF terminal.
    loop {
        conn.read_exact(&mut buf)?;
        total += PREFERRED_CHUNK_SIZE as u64;
        if *buf.last().unwrap() == 0xFF {
            break;
        }
    }

    conn.write_line("OK")?;

    let time_line  = conn.read_line()?; // "TIME <ns>"
    let elapsed_ns = parse_time_ns(&time_line)?;

    println!(
        "  download  {:.2} Mbit/s  ({total} bytes in {:.3}s, client={:.3}s)",
        total as f64 * 8.0 / (elapsed_ns as f64 / 1e9) / 1_000_000.0,
        elapsed_ns as f64 / 1e9,
        t0.elapsed().as_secs_f64(),
    );

    Ok(TransferResult { bytes: total, elapsed_ns })
}

// ─── Upload (PUTNORESULT) ─────────────────────────────────────────────────────

/// Issue a PUTNORESULT command and send random chunks for `duration_secs`.
///
/// The reference client uses PUT (with intermediate TIME BYTES responses) for
/// the full upload, which requires a separate reader thread.  PUTNORESULT gives
/// an identical final result without that complexity.
pub fn run_upload(conn: &mut RmbtConn, duration_secs: u32) -> Result<TransferResult> {
    let accept = conn.read_accept()?;
    if !accept.contains("PUT") {
        bail!("expected ACCEPT with PUT/PUTNORESULT, got: {accept}");
    }

    conn.write_line(&format!("PUTNORESULT {PREFERRED_CHUNK_SIZE}"))?;

    let ok = conn.read_line()?;
    if ok != "OK" {
        bail!("expected OK after PUTNORESULT, got: {ok}");
    }

    // Pre-fill a buffer with random bytes; reuse for every chunk.
    let mut chunk = vec![0u8; PREFERRED_CHUNK_SIZE];
    fastrand::fill(&mut chunk);

    let deadline  = Instant::now() + std::time::Duration::from_secs(duration_secs as u64);
    let t0        = Instant::now();
    let mut total = 0u64;

    loop {
        let terminal = Instant::now() >= deadline;
        *chunk.last_mut().unwrap() = if terminal { 0xFF } else { 0x00 };
        conn.write_bytes(&chunk)?;
        total += PREFERRED_CHUNK_SIZE as u64;
        if terminal {
            break;
        }
    }
    // Flush the TLS write buffer so the server receives the terminal chunk.
    conn.flush()?;

    let time_line  = conn.read_line()?; // "TIME <ns>"
    let elapsed_ns = parse_time_ns(&time_line)?;

    println!(
        "  upload    {:.2} Mbit/s  ({total} bytes in {:.3}s, client={:.3}s)",
        total as f64 * 8.0 / (elapsed_ns as f64 / 1e9) / 1_000_000.0,
        elapsed_ns as f64 / 1e9,
        t0.elapsed().as_secs_f64(),
    );

    Ok(TransferResult { bytes: total, elapsed_ns })
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

fn parse_time_ns(line: &str) -> Result<u64> {
    // "TIME 123456789" or "TIME 123456789 BYTES 98765"
    line.split_whitespace()
        .nth(1)
        .and_then(|s| s.parse::<u64>().ok())
        .ok_or_else(|| anyhow::anyhow!("invalid TIME line: {line}"))
}
