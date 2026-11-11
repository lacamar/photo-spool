//! iOS drops Wi-Fi service connections (AFC included) unless the host keeps a
//! `com.apple.mobile.heartbeat` session alive, answering each "Marco" with "Polo". Real usbmuxd
//! does this for network devices; the stand-in muxer in `wifi` does it here, one thread per phone.
use std::io::{Read, Write};
use std::net::{IpAddr, SocketAddr, TcpStream};
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use plist::{Dictionary, Value};
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, PrivateKeyDer, ServerName, UnixTime};
use rustls::{ClientConfig, ClientConnection, DigitallySignedStruct, SignatureScheme, StreamOwned};

const LOCKDOWN_PORT: u16 = 62078;
const IO_TIMEOUT: Duration = Duration::from_secs(40);
const RETRY: Duration = Duration::from_secs(5);
const MAX_FRAME: usize = 1 << 20;

type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;

trait Stream: Read + Write + Send {}
impl<T: Read + Write + Send> Stream for T {}

fn send(s: &mut dyn Stream, msg: Dictionary) -> Result<()> {
    let mut body = Vec::new();
    Value::Dictionary(msg).to_writer_xml(&mut body)?;
    s.write_all(&(body.len() as u32).to_be_bytes())?;
    s.write_all(&body)?;
    Ok(s.flush()?)
}

fn recv(s: &mut dyn Stream) -> Result<Dictionary> {
    let mut len = [0u8; 4];
    s.read_exact(&mut len)?;
    let len = u32::from_be_bytes(len) as usize;
    if len > MAX_FRAME {
        return Err(format!("oversized frame: {len} bytes").into());
    }
    let mut body = vec![0; len];
    s.read_exact(&mut body)?;
    Ok(plist::from_bytes(&body)?)
}

fn dict<const N: usize>(pairs: [(&str, Value); N]) -> Dictionary {
    pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect()
}

/// Trust is the pair record: the phone must present exactly its recorded DeviceCertificate.
#[derive(Debug)]
struct PairedDevice {
    provider: Arc<rustls::crypto::CryptoProvider>,
    device_cert: CertificateDer<'static>,
}

impl ServerCertVerifier for PairedDevice {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        _: &[CertificateDer<'_>],
        _: &ServerName<'_>,
        _: &[u8],
        _: UnixTime,
    ) -> std::result::Result<ServerCertVerified, rustls::Error> {
        if end_entity.as_ref() == self.device_cert.as_ref() {
            Ok(ServerCertVerified::assertion())
        } else {
            Err(rustls::Error::InvalidCertificate(
                rustls::CertificateError::UnknownIssuer,
            ))
        }
    }

    fn verify_tls12_signature(
        &self,
        msg: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls12_signature(
            msg,
            cert,
            dss,
            &self.provider.signature_verification_algorithms,
        )
    }

    fn verify_tls13_signature(
        &self,
        msg: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls13_signature(
            msg,
            cert,
            dss,
            &self.provider.signature_verification_algorithms,
        )
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.provider
            .signature_verification_algorithms
            .supported_schemes()
    }
}

struct Pairing {
    host_id: String,
    buid: String,
    tls: Arc<ClientConfig>,
}

fn load_pairing(record: &Path) -> Result<Pairing> {
    let pair: Dictionary = plist::from_file(record)?;
    let field = |k: &str| pair.get(k).ok_or_else(|| format!("pair record lacks {k}"));
    let data =
        |k: &str| field(k).and_then(|v| v.as_data().ok_or_else(|| format!("{k} is not data")));
    let string = |k: &str| {
        field(k).and_then(|v| {
            v.as_string()
                .map(String::from)
                .ok_or_else(|| format!("{k} is not a string"))
        })
    };
    let cert = CertificateDer::from_pem_slice(data("HostCertificate")?)?;
    let key = PrivateKeyDer::from_pem_slice(data("HostPrivateKey")?)?;
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let tls = ClientConfig::builder_with_provider(provider.clone())
        .with_safe_default_protocol_versions()?
        .dangerous()
        .with_custom_certificate_verifier(Arc::new(PairedDevice {
            provider,
            device_cert: CertificateDer::from_pem_slice(data("DeviceCertificate")?)?,
        }))
        .with_client_auth_cert(vec![cert], key)?;
    Ok(Pairing {
        host_id: string("HostID")?,
        buid: string("SystemBUID")?,
        tls: Arc::new(tls),
    })
}

fn tls(pairing: &Pairing, tcp: TcpStream) -> Result<Box<dyn Stream>> {
    let conn = ClientConnection::new(pairing.tls.clone(), ServerName::try_from("device")?)?;
    Ok(Box::new(StreamOwned::new(conn, tcp)))
}

fn connect(ip: IpAddr, scope: u32, port: u16) -> Result<TcpStream> {
    let addr = match ip {
        IpAddr::V6(v6) => SocketAddr::V6(std::net::SocketAddrV6::new(v6, port, 0, scope)),
        v4 => SocketAddr::new(v4, port),
    };
    let s = TcpStream::connect_timeout(&addr, Duration::from_secs(10))?;
    s.set_read_timeout(Some(IO_TIMEOUT))?;
    s.set_write_timeout(Some(IO_TIMEOUT))?;
    Ok(s)
}

/// Runs one heartbeat session until it fails or `stop` is set; sets `alive` once the phone pings.
fn session(
    record: &Path,
    ip: IpAddr,
    scope: u32,
    stop: &AtomicBool,
    alive: &AtomicBool,
) -> Result<()> {
    let pairing = load_pairing(record)?;
    let mut tcp = connect(ip, scope, LOCKDOWN_PORT)?;
    let label = || Value::String("photo-spool".into());
    send(
        &mut tcp,
        dict([
            ("Label", label()),
            ("Request", Value::String("StartSession".into())),
            ("HostID", Value::String(pairing.host_id.clone())),
            ("SystemBUID", Value::String(pairing.buid.clone())),
        ]),
    )?;
    let reply = recv(&mut tcp)?;
    if let Some(err) = reply.get("Error").and_then(Value::as_string) {
        return Err(format!("StartSession: {err}").into());
    }
    // the session upgrades the same socket to TLS in place
    let mut ld = match reply.get("EnableSessionSSL").and_then(Value::as_boolean) {
        Some(true) => tls(&pairing, tcp)?,
        _ => Box::new(tcp),
    };
    send(
        &mut *ld,
        dict([
            ("Label", label()),
            ("Request", Value::String("StartService".into())),
            (
                "Service",
                Value::String("com.apple.mobile.heartbeat".into()),
            ),
        ]),
    )?;
    let svc = recv(&mut *ld)?;
    let port = svc
        .get("Port")
        .and_then(Value::as_unsigned_integer)
        .ok_or_else(|| format!("StartService: {svc:?}"))? as u16;
    let raw = connect(ip, scope, port)?;
    let mut hb = if svc.get("EnableServiceSSL").and_then(Value::as_boolean) == Some(true) {
        tls(&pairing, raw)?
    } else {
        Box::new(raw)
    };
    while !stop.load(Ordering::Relaxed) {
        let msg = recv(&mut *hb)?;
        match msg.get("Command").and_then(Value::as_string) {
            Some("Marco") => {
                alive.store(true, Ordering::Relaxed);
                send(&mut *hb, dict([("Command", Value::String("Polo".into()))]))?;
            }
            other => return Err(format!("heartbeat ended: {other:?}").into()),
        }
    }
    Ok(())
}

/// A running heartbeat for one phone; stops on drop.
pub struct Heartbeat {
    pub addr: (IpAddr, u32),
    stop: Arc<AtomicBool>,
    alive: Arc<AtomicBool>,
}

impl Heartbeat {
    pub fn start(record: std::path::PathBuf, addr: (IpAddr, u32)) -> Self {
        let stop = Arc::new(AtomicBool::new(false));
        let alive = Arc::new(AtomicBool::new(false));
        let (s, a) = (stop.clone(), alive.clone());
        std::thread::spawn(move || {
            while !s.load(Ordering::Relaxed) {
                if let Err(e) = session(&record, addr.0, addr.1, &s, &a) {
                    log::info!("Wi-Fi heartbeat to {}: {e}", addr.0);
                }
                a.store(false, Ordering::Relaxed);
                std::thread::sleep(RETRY);
            }
        });
        Self { addr, stop, alive }
    }

    /// True once the phone is answering pings; services (AFC) are usable from then on.
    pub fn alive(&self) -> bool {
        self.alive.load(Ordering::Relaxed)
    }
}

impl Drop for Heartbeat {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
    }
}
