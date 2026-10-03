// Network policy — every request Coucou makes goes through here.
//
// Nothing leaves the machine unless it is on the allow-list below. Each entry
// is a security assessment of one service: where it lives (exact host, HTTPS
// only, port 443), what Coucou may ask it (method + path), and how sensitive
// the data that travels with the request is. Anything that does not match is
// refused before a socket is opened, and the refusal is logged.
//
// Three layers, so one mistake in a call site cannot leak a key:
//   1. `get` / `post` check the URL against the table (scheme, userinfo, host,
//      port, method, path).
//   2. The client resolves names only for allow-listed hosts, and refuses
//      private, loopback and link-local answers (DNS rebinding).
//   3. Redirects are never followed, so a key is never replayed to another host.
//
// System and environment proxies (HTTPS_PROXY…) are not used: a proxy resolves
// names itself and would bypass layer 2.

use std::error::Error;
use std::net::{IpAddr, SocketAddr};
use std::collections::HashSet;
use std::sync::{LazyLock, Mutex};
use std::time::Duration;

use reqwest::dns::{Addrs, Name, Resolve, Resolving};
use reqwest::{Client, Method, RequestBuilder, Url};

use crate::log;

/// What a request can disclose, from the least to the most sensitive.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Sensitivity {
    /// A read-only token scoped by the user (GitHub).
    Token,
    /// A secret key that can move money (Stripe `sk_`).
    SecretKey,
    /// An API key plus the content of the user's chats and dropped files (Claude).
    KeyAndContent,
}

struct Rule {
    service: &'static str,
    host: &'static str,
    /// (method, exact path). No prefixes: `/user` must not open `/user/keys`.
    routes: &'static [(&'static str, &'static str)],
    sensitivity: Sensitivity,
    /// Responses bigger than this are dropped.
    max_response_bytes: usize,
}

/// The whole allow-list. Adding a service means adding a line here and nowhere else.
/// Vercel, Resend, Notion, Cal.com, n8n and OpenAI/Gemini are deliberately absent.
const RULES: &[Rule] = &[
    Rule {
        service: "Claude (Anthropic)",
        host: "api.anthropic.com",
        routes: &[("POST", "/v1/messages")],
        sensitivity: Sensitivity::KeyAndContent,
        max_response_bytes: 8 * 1024 * 1024,
    },
    Rule {
        service: "Stripe",
        host: "api.stripe.com",
        routes: &[("GET", "/v1/balance"), ("GET", "/v1/charges")],
        sensitivity: Sensitivity::SecretKey,
        max_response_bytes: 1024 * 1024,
    },
    Rule {
        service: "GitHub",
        host: "api.github.com",
        routes: &[("GET", "/user"), ("GET", "/user/repos")],
        sensitivity: Sensitivity::Token,
        max_response_bytes: 2 * 1024 * 1024,
    },
];

/// Integration ids that may reach the network at all.
const ALLOWED_INTEGRATIONS: &[&str] = &["integration_stripe", "integration_github"];

pub fn integration_allowed(id: &str) -> bool {
    ALLOWED_INTEGRATIONS.contains(&id)
}

/// A request that passed the assessment.
#[derive(Debug)]
pub struct Approved {
    pub url: Url,
    pub service: &'static str,
    pub sensitivity: Sensitivity,
}

/// Decides whether a request may be sent. Pure: no I/O, so it is unit-tested.
pub fn assess(method: &Method, raw_url: &str) -> Result<Approved, String> {
    let url = Url::parse(raw_url).map_err(|_| "not a valid URL".to_string())?;
    if url.scheme() != "https" {
        return Err(format!("{} is not HTTPS", url.scheme()));
    }
    if !url.username().is_empty() || url.password().is_some() {
        return Err("credentials in the URL".into());
    }
    if url.fragment().is_some() {
        return Err("fragment in the URL".into());
    }
    if url.port().is_some_and(|p| p != 443) {
        return Err("non-standard port".into());
    }
    let host = url.host_str().ok_or("no host")?.to_ascii_lowercase();
    // `Url` already normalised IDNA / percent-encoding, so an exact match is safe.
    let rule = RULES
        .iter()
        .find(|r| r.host == host)
        .ok_or_else(|| format!("{host} is not on the allow-list"))?;
    if url.path().contains("//") || url.path().contains("/../") || url.path().contains("%2e") {
        return Err("suspicious path".into());
    }
    if !rule
        .routes
        .iter()
        .any(|(m, p)| *m == method.as_str() && url.path() == *p)
    {
        return Err(format!("{} {} is not an allowed call for {}", method, url.path(), rule.service));
    }
    Ok(Approved {
        url,
        service: rule.service,
        sensitivity: rule.sensitivity,
    })
}

/// True for addresses a public API must never resolve to.
fn is_internal(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => {
            let o = v4.octets();
            v4.is_loopback()
                || v4.is_private()
                || v4.is_link_local()
                || v4.is_unspecified()
                || v4.is_broadcast()
                || v4.is_multicast()
                || o[0] == 0
                || o[0] >= 240 // reserved
                || (o[0] == 100 && (64..128).contains(&o[1])) // CGNAT
                || (o[0] == 192 && o[1] == 0 && o[2] == 0) // IETF protocol assignments
                || (o[0] == 198 && (o[1] == 18 || o[1] == 19)) // benchmarking
        }
        IpAddr::V6(v6) => {
            let s = v6.segments();
            // Anything that embeds an IPv4 address is judged by that address.
            if let Some(v4) = v6.to_ipv4_mapped() {
                return is_internal(IpAddr::V4(v4));
            }
            let embedded = |hi: u16, lo: u16| std::net::Ipv4Addr::new((hi >> 8) as u8, hi as u8, (lo >> 8) as u8, lo as u8);
            if s[..6] == [0, 0, 0, 0, 0, 0] {
                return true; // ::, ::1 and the IPv4-compatible block
            }
            if s[0] == 0x0064 && s[1] == 0xff9b && s[2..6] == [0, 0, 0, 0] {
                return is_internal(IpAddr::V4(embedded(s[6], s[7]))); // NAT64
            }
            if s[0] == 0x2002 {
                return is_internal(IpAddr::V4(embedded(s[1], s[2]))); // 6to4
            }
            (s[0] & 0xff00) == 0xff00 // multicast
                || (s[0] & 0xfe00) == 0xfc00 // unique local
                || (s[0] & 0xffc0) == 0xfe80 // link-local
                || (s[0] & 0xffc0) == 0xfec0 // site-local
        }
    }
}

struct AllowListResolver;

impl Resolve for AllowListResolver {
    fn resolve(&self, name: Name) -> Resolving {
        let host = name.as_str().to_ascii_lowercase();
        Box::pin(async move {
            if !RULES.iter().any(|r| r.host == host) {
                log::line(format!("net  blocked DNS lookup for {host}"));
                return Err(blocked(format!("{host} is not on the allow-list")));
            }
            let found: Vec<SocketAddr> = tokio::net::lookup_host((host.as_str(), 443))
                .await
                .map_err(|e| Box::new(e) as Box<dyn Error + Send + Sync>)?
                .filter(|a| !is_internal(a.ip()))
                .collect();
            if found.is_empty() {
                log::line(format!("net  {host} resolved only to internal addresses; refused"));
                return Err(blocked(format!("{host} resolves to an internal address")));
            }
            Ok(Box::new(found.into_iter()) as Addrs)
        })
    }
}

fn blocked(reason: String) -> Box<dyn Error + Send + Sync> {
    format!("blocked by Coucou's network policy: {reason}").into()
}

static SEEN_SERVICES: LazyLock<Mutex<HashSet<&'static str>>> = LazyLock::new(|| Mutex::new(HashSet::new()));

static CLIENT: LazyLock<Client> = LazyLock::new(|| {
    Client::builder()
        .https_only(true)
        // A proxy would resolve names itself and skip the checks above, so none is used.
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .min_tls_version(reqwest::tls::Version::TLS_1_2)
        .dns_resolver(std::sync::Arc::new(AllowListResolver))
        .connect_timeout(Duration::from_secs(10))
        .user_agent("Coucou")
        .build()
        .expect("static client configuration is valid")
});

/// Per-request limit; the Claude call raises it for long answers.
pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(10);

/// The URL a refused request is pointed at. `.invalid` never resolves, and the
/// resolver above refuses it anyway, so a refused call fails like a dead network
/// and every caller's existing error path handles it.
const REFUSED: &str = "https://refused.invalid/";

fn guarded(method: Method, raw_url: &str) -> RequestBuilder {
    match assess(&method, raw_url) {
        Ok(approved) => {
            // One line per service per session: an audit trail without log spam.
            if SEEN_SERVICES.lock().map(|mut seen| seen.insert(approved.service)).unwrap_or(false) {
                log::line(format!("net  {} allowed ({:?})", approved.service, approved.sensitivity));
            }
            CLIENT.request(method, approved.url).timeout(DEFAULT_TIMEOUT)
        }
        Err(reason) => {
            // Log the host only: a path or query can carry an id.
            let host = Url::parse(raw_url)
                .ok()
                .and_then(|u| u.host_str().map(str::to_string))
                .unwrap_or_default();
            log::line(format!("net  BLOCKED {method} {host}: {reason}"));
            CLIENT.request(method, REFUSED).timeout(DEFAULT_TIMEOUT)
        }
    }
}

pub fn get(url: &str) -> RequestBuilder {
    guarded(Method::GET, url)
}

pub fn post(url: &str) -> RequestBuilder {
    guarded(Method::POST, url)
}

/// Reads a response body, refusing anything over `limit` bytes.
pub async fn read_capped(mut response: reqwest::Response, limit: usize) -> Result<Vec<u8>, String> {
    if response.content_length().is_some_and(|n| n as usize > limit) {
        return Err("response too large".into());
    }
    let mut body = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(|e| e.to_string())? {
        if body.len() + chunk.len() > limit {
            return Err("response too large".into());
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

/// Reads a JSON body under the service's cap; anything unreadable or oversized is `{}`,
/// which is what the pollers already treat as "no data".
pub async fn read_json(response: reqwest::Response) -> serde_json::Value {
    let limit = limit_for(response.url().as_str());
    match read_capped(response, limit).await {
        Ok(bytes) => serde_json::from_slice(&bytes).unwrap_or_else(|_| serde_json::json!({})),
        Err(e) => {
            log::line(format!("net  dropped a response: {e}"));
            serde_json::json!({})
        }
    }
}

/// Response size limit for a URL that was approved earlier (Claude chat reads its
/// own reply with this).
pub fn limit_for(raw_url: &str) -> usize {
    Url::parse(raw_url)
        .ok()
        .and_then(|u| u.host_str().and_then(|h| RULES.iter().find(|r| r.host == h)))
        .map(|r| r.max_response_bytes)
        .unwrap_or(1024 * 1024)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ok(method: Method, url: &str) -> bool {
        assess(&method, url).is_ok()
    }

    #[test]
    fn allows_exactly_the_calls_coucou_makes() {
        assert!(ok(Method::POST, "https://api.anthropic.com/v1/messages"));
        assert!(ok(Method::GET, "https://api.stripe.com/v1/balance"));
        assert!(ok(Method::GET, "https://api.stripe.com/v1/charges?limit=3"));
        assert!(ok(Method::GET, "https://api.github.com/user"));
        assert!(ok(Method::GET, "https://api.github.com/user/repos?per_page=100&affiliation=owner&sort=pushed"));
    }

    #[test]
    fn refuses_other_services() {
        for url in [
            "https://api.openai.com/v1/chat/completions",
            "https://generativelanguage.googleapis.com/v1beta/openai/models",
            "https://api.vercel.com/v6/deployments",
            "https://api.resend.com/emails",
            "https://api.notion.com/v1/search",
            "https://api.cal.com/v2/bookings",
            "https://evil.example/v1/messages",
        ] {
            assert!(!ok(Method::GET, url) && !ok(Method::POST, url), "{url}");
        }
    }

    #[test]
    fn refuses_host_tricks() {
        for url in [
            "https://api.anthropic.com.evil.example/v1/messages",
            "https://evil.example/https://api.anthropic.com/v1/messages",
            "https://api.anthropic.com@evil.example/v1/messages",
            "https://api.anthropic.com:evil@evil.example/v1/messages",
            "https://user:pw@api.anthropic.com/v1/messages",
            "https://api.anthropic.com:8443/v1/messages",
            "https://xapi.anthropic.com/v1/messages",
            "https://api.anthropic.com.:444/v1/messages",
            "https://127.0.0.1/v1/messages",
            "https://[::1]/v1/messages",
            "https://169.254.169.254/latest/meta-data",
        ] {
            assert!(!ok(Method::POST, url), "{url}");
        }
    }

    #[test]
    fn refuses_plain_http_and_other_schemes() {
        assert!(!ok(Method::POST, "http://api.anthropic.com/v1/messages"));
        assert!(!ok(Method::GET, "ftp://api.stripe.com/v1/balance"));
        assert!(!ok(Method::GET, "file:///C:/Users/me/.claude/settings.json"));
        assert!(!ok(Method::GET, "not a url"));
    }

    #[test]
    fn refuses_wrong_method_or_path() {
        assert!(!ok(Method::GET, "https://api.anthropic.com/v1/messages"));
        assert!(!ok(Method::POST, "https://api.stripe.com/v1/balance"));
        assert!(!ok(Method::GET, "https://api.stripe.com/v1/customers"));
        assert!(!ok(Method::GET, "https://api.stripe.com/v1/balance_transactions"));
        assert!(!ok(Method::GET, "https://api.github.com/user/keys"));
        assert!(!ok(Method::GET, "https://api.github.com/users/octocat"));
        assert!(!ok(Method::GET, "https://api.github.com/user/../repos/x/y"));
        assert!(!ok(Method::GET, "https://api.github.com//user"));
        assert!(!ok(Method::GET, "https://api.anthropic.com/v1/messages#frag"));
    }

    #[test]
    fn host_match_is_case_insensitive_but_exact() {
        assert!(ok(Method::GET, "https://API.GitHub.com/user"));
        assert!(!ok(Method::GET, "https://github.com/user"));
    }

    #[test]
    fn only_stripe_and_github_pollers_run() {
        assert!(integration_allowed("integration_stripe"));
        assert!(integration_allowed("integration_github"));
        for id in ["integration_vercel", "integration_resend", "integration_notion", "integration_calcom", "integration_n8n"] {
            assert!(!integration_allowed(id), "{id}");
        }
    }

    #[test]
    fn internal_addresses_are_recognised() {
        for ip in ["127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1", "fe80::1", "fd00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe", "2002:7f00:1::1", "2002:a9fe:a9fe::1", "::127.0.0.1", "ff02::1", "fec0::1", "224.0.0.1", "240.0.0.1", "198.18.0.1", "192.0.0.1"] {
            assert!(is_internal(ip.parse().unwrap()), "{ip}");
        }
        for ip in ["8.8.8.8", "160.79.104.10", "140.82.112.5", "2606:4700::1111", "64:ff9b::808:808", "2002:808:808::1"] {
            assert!(!is_internal(ip.parse().unwrap()), "{ip}");
        }
    }

    #[tokio::test]
    async fn refused_requests_never_connect() {
        let err = get("https://evil.example/steal").send().await.unwrap_err();
        assert!(err.is_connect() || err.is_request(), "{err}");
    }
}
