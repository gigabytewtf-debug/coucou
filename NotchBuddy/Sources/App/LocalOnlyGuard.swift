import Foundation

#if LOCAL_ONLY
/// Local-only build: every URLSession request to a host other than this Mac is refused before
/// it leaves the process. Ollama and LM Studio on 127.0.0.1 / localhost keep working; Claude,
/// OpenAI, Gemini, Stripe, GitHub and the other cloud services do not.
final class LocalOnlyGuard: URLProtocol {
    static func install() { URLProtocol.registerClass(LocalOnlyGuard.self) }

    private static func isLoopback(_ host: String?) -> Bool {
        guard let host = host?.lowercased() else { return false }
        return host == "localhost" || host == "127.0.0.1" || host == "::1" || host == "[::1]"
    }

    override class func canInit(with request: URLRequest) -> Bool {
        guard let scheme = request.url?.scheme?.lowercased(), scheme == "http" || scheme == "https" else { return false }
        return !isLoopback(request.url?.host)
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        appendAppLog("local-only.log", "blocked request to \(request.url?.host ?? "?")")
        client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet))
    }

    override func stopLoading() {}
}
#endif
