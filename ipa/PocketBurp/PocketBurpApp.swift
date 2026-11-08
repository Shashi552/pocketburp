//
//  PocketBurpApp.swift
//  PocketBurp
//
//  Native UIKit shell around the PocketBurp GUI.
//
//  Why a shell at all? A proxy must *listen* on a TCP port and iOS forbids that
//  for web apps, so the engine has to live in a real app process (or in iSH).
//  This shell owns the window, loads http://127.0.0.1:9090, and — if the engine
//  is not up yet — shows a native recovery screen with a one-tap way to start it
//  in iSH instead of a blank white page.
//
//  Builds and runs as-is with no third-party dependencies. When you add the
//  iSH xcframework, wire it up in EngineBridge.launchPythonEngine().
//

import UIKit
import WebKit

private let kGUIURL = "http://127.0.0.1:9090/?src=app"
private let kBrandOrange = UIColor(red: 1.0, green: 0.4, blue: 0.2, alpha: 1.0)
private let kBG = UIColor(red: 0.118, green: 0.118, blue: 0.118, alpha: 1.0)
private let kPanel = UIColor(red: 0.176, green: 0.176, blue: 0.176, alpha: 1.0)

@main
final class AppDelegate: UIResponder, UIApplicationDelegate {
    var window: UIWindow?

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions:
                        [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        let window = UIWindow(frame: UIScreen.main.bounds)
        window.rootViewController = RootViewController()
        window.makeKeyAndVisible()
        self.window = window
        return true
    }
}

// MARK: - Root

final class RootViewController: UIViewController {

    private let engine = EngineBridge()
    private var webView: WKWebView!
    private let splash = SplashView()
    private let offlineView = OfflineView()
    private var retriesLeft = 30

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = kBG
        buildWebView()
        buildSplash()
        offlineView.isHidden = true
        offlineView.onRetry = { [weak self] in self?.startEngineFlow() }
        offlineView.onOpenISH = { [weak self] in self?.openISH() }
        startEngineFlow()
    }

    // MARK: setup

    private func buildWebView() {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true
        config.mediaTypesRequiringUserActionForPlayback = []

        webView = WKWebView(frame: .zero, configuration: config)
        webView.translatesAutoresizingMaskIntoConstraints = false
        webView.navigationDelegate = self
        webView.isOpaque = false
        webView.backgroundColor = kBG
        webView.scrollView.backgroundColor = kBG
        webView.scrollView.bounces = false
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        webView.allowsBackForwardNavigationGestures = false
        if #available(iOS 16.4, *) { webView.isInspectable = true }
        view.addSubview(webView)

        offlineView.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(offlineView)

        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: view.topAnchor),
            webView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            webView.bottomAnchor.constraint(equalTo: view.bottomAnchor),

            offlineView.topAnchor.constraint(equalTo: view.topAnchor),
            offlineView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            offlineView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            offlineView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
    }

    private func buildSplash() {
        splash.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(splash)
        NSLayoutConstraint.activate([
            splash.topAnchor.constraint(equalTo: view.topAnchor),
            splash.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            splash.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            splash.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
    }

    // MARK: flow

    private func startEngineFlow() {
        offlineView.isHidden = true
        retriesLeft = 30
        splash.isHidden = false
        splash.alpha = 1

        engine.start { [weak self] result in
            DispatchQueue.main.async {
                guard let self = self else { return }
                if case .failure = result {
                    // Not fatal: the GUI may still be served by iSH.
                    self.engine.lastErrorText = (try? result.get()) == nil
                        ? "engine not running" : ""
                }
                self.loadGUI()
            }
        }
    }

    private func loadGUI() {
        guard retriesLeft > 0 else {
            showOffline(reason: engine.lastErrorText)
            return
        }
        retriesLeft -= 1
        var req = URLRequest(url: URL(string: kGUIURL)!)
        req.timeoutInterval = 8
        req.cachePolicy = .reloadIgnoringLocalCacheData
        webView.load(req)
    }

    private func showOffline(reason: String) {
        offlineView.isHidden = false
        offlineView.reason = reason
        UIView.animate(withDuration: 0.2) { self.splash.alpha = 0 } completion: { _ in
            self.splash.isHidden = true
        }
    }

    private func openISH() {
        // iSH registers the ish:// scheme; fall back to copied instructions.
        if let url = URL(string: "ish://"), UIApplication.shared.canOpenURL(url) {
            UIApplication.shared.open(url, options: [:], completionHandler: nil)
        } else {
            let alert = UIAlertController(
                title: "Start the engine in iSH",
                message: "Open iSH Shell and run:\n\ncd ~/pocketburp\nsh run.sh\n\n"
                       + "The command is on your clipboard.",
                preferredStyle: .alert)
            UIPasteboard.general.string = "cd ~/pocketburp && sh run.sh"
            alert.addAction(UIAlertAction(title: "OK", style: .default))
            present(alert, animated: true)
        }
    }
}

// MARK: - WKNavigationDelegate

extension RootViewController: WKNavigationDelegate {
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        offlineView.isHidden = true
        UIView.animate(withDuration: 0.25) { self.splash.alpha = 0 } completion: { _ in
            self.splash.isHidden = true
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        scheduleRetry()
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        scheduleRetry()
    }

    private func scheduleRetry() {
        guard offlineView.isHidden else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            self?.loadGUI()
        }
    }
}

// MARK: - Splash

final class SplashView: UIView {
    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = kBG

        let mark = UILabel()
        mark.text = "⚡"
        mark.font = .systemFont(ofSize: 44)
        mark.textAlignment = .center
        mark.translatesAutoresizingMaskIntoConstraints = false
        addSubview(mark)

        let label = UILabel()
        let s = NSMutableAttributedString(string: "Pocket",
                                          attributes: [.foregroundColor: UIColor.white,
                                                       .font: UIFont.systemFont(ofSize: 32, weight: .heavy)])
        s.append(NSAttributedString(string: "Burp",
                                    attributes: [.foregroundColor: kBrandOrange,
                                                 .font: UIFont.systemFont(ofSize: 32, weight: .heavy)]))
        label.attributedText = s
        label.textAlignment = .center
        label.translatesAutoresizingMaskIntoConstraints = false
        addSubview(label)

        let sub = UILabel()
        sub.text = "INTERCEPT · EDIT · REPLAY · FUZZ"
        sub.font = .systemFont(ofSize: 9.5, weight: .semibold)
        sub.textColor = UIColor(white: 0.42, alpha: 1)
        sub.textAlignment = .center
        sub.translatesAutoresizingMaskIntoConstraints = false
        addSubview(sub)

        let spinner = UIActivityIndicatorView(style: .medium)
        spinner.color = kBrandOrange
        spinner.startAnimating()
        spinner.translatesAutoresizingMaskIntoConstraints = false
        addSubview(spinner)

        NSLayoutConstraint.activate([
            mark.centerXAnchor.constraint(equalTo: centerXAnchor),
            mark.centerYAnchor.constraint(equalTo: centerYAnchor, constant: -58),
            label.centerXAnchor.constraint(equalTo: centerXAnchor),
            label.topAnchor.constraint(equalTo: mark.bottomAnchor, constant: 4),
            sub.centerXAnchor.constraint(equalTo: centerXAnchor),
            sub.topAnchor.constraint(equalTo: label.bottomAnchor, constant: 4),
            spinner.centerXAnchor.constraint(equalTo: centerXAnchor),
            spinner.topAnchor.constraint(equalTo: sub.bottomAnchor, constant: 26),
        ])
    }

    required init?(coder: NSCoder) { fatalError("unused") }
}

// MARK: - Offline / recovery screen

final class OfflineView: UIView {
    var onRetry: (() -> Void)?
    var onOpenISH: (() -> Void)?
    private let reasonLabel = UILabel()

    var reason: String = "" {
        didSet { reasonLabel.text = reason.isEmpty ? "" : "Details: \(reason)" }
    }

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = kBG

        let card = UIView()
        card.backgroundColor = kPanel
        card.layer.cornerRadius = 14
        card.translatesAutoresizingMaskIntoConstraints = false
        addSubview(card)

        let title = UILabel()
        title.text = "Engine not running"
        title.font = .systemFont(ofSize: 20, weight: .bold)
        title.textColor = .white
        title.textAlignment = .center
        title.translatesAutoresizingMaskIntoConstraints = false
        card.addSubview(title)

        let body = UILabel()
        body.numberOfLines = 0
        body.textColor = UIColor(white: 0.72, alpha: 1)
        body.font = .systemFont(ofSize: 13.5)
        body.text = """
        PocketBurp's proxy engine has to listen on ports 8080 and 9090, which iOS
        only allows for a real process. Start it once in iSH Shell:

            cd ~/pocketburp
            sh run.sh

        Then come back — this app is the fullscreen GUI for it.
        """
        body.translatesAutoresizingMaskIntoConstraints = false
        card.addSubview(body)

        reasonLabel.font = .systemFont(ofSize: 11)
        reasonLabel.textColor = UIColor(red: 0.85, green: 0.65, blue: 0.35, alpha: 1)
        reasonLabel.numberOfLines = 0
        reasonLabel.textAlignment = .center
        reasonLabel.translatesAutoresizingMaskIntoConstraints = false
        card.addSubview(reasonLabel)

        let retry = makeButton("Retry", fill: kBrandOrange, textColor: .black)
        retry.addTarget(self, action: #selector(retryTapped), for: .touchUpInside)
        card.addSubview(retry)

        let open = makeButton("Open iSH Shell", fill: UIColor(white: 0.27, alpha: 1), textColor: .white)
        open.addTarget(self, action: #selector(ishTapped), for: .touchUpInside)
        card.addSubview(open)

        for v in [title, body, reasonLabel, retry, open] {
            v.translatesAutoresizingMaskIntoConstraints = false
        }

        NSLayoutConstraint.activate([
            card.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 18),
            card.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -18),
            card.centerYAnchor.constraint(equalTo: centerYAnchor),

            title.topAnchor.constraint(equalTo: card.topAnchor, constant: 22),
            title.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: 18),
            title.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -18),

            body.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 12),
            body.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            body.trailingAnchor.constraint(equalTo: title.trailingAnchor),

            reasonLabel.topAnchor.constraint(equalTo: body.bottomAnchor, constant: 10),
            reasonLabel.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            reasonLabel.trailingAnchor.constraint(equalTo: title.trailingAnchor),

            retry.topAnchor.constraint(equalTo: reasonLabel.bottomAnchor, constant: 18),
            retry.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            retry.trailingAnchor.constraint(equalTo: title.trailingAnchor),
            retry.heightAnchor.constraint(equalToConstant: 46),

            open.topAnchor.constraint(equalTo: retry.bottomAnchor, constant: 9),
            open.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            open.trailingAnchor.constraint(equalTo: title.trailingAnchor),
            open.heightAnchor.constraint(equalToConstant: 46),
            open.bottomAnchor.constraint(equalTo: card.bottomAnchor, constant: -20),
        ])
    }

    required init?(coder: NSCoder) { fatalError("unused") }

    private func makeButton(_ title: String, fill: UIColor, textColor: UIColor) -> UIButton {
        let b = UIButton(type: .system)
        b.setTitle(title, for: .normal)
        b.setTitleColor(textColor, for: .normal)
        b.titleLabel?.font = .systemFont(ofSize: 15, weight: .semibold)
        b.backgroundColor = fill
        b.layer.cornerRadius = 10
        return b
    }

    @objc private func retryTapped() { onRetry?() }
    @objc private func ishTapped() { onOpenISH?() }
}

// MARK: - Engine bridge

enum EngineError: LocalizedError {
    case missingBinary
    case timeout

    var errorDescription: String? {
        switch self {
        case .missingBinary:
            return "embedded iSH engine not bundled (webview-only build)"
        case .timeout:
            return "no answer on 127.0.0.1:9090 after 25s"
        }
    }
}

/// Integration point for the embedded Alpine/Python engine.
///
/// Shipping build: `canStartEmbedded == false`, so the app immediately probes
/// 127.0.0.1:9090 — which is where `sh run.sh` inside iSH Shell listens.
final class EngineBridge {

    static let proxyPort: UInt16 = 8080
    static let guiPort: UInt16 = 9090
    var lastErrorText: String = ""

    var canStartEmbedded: Bool {
        Bundle.main.url(forResource: "ish", withExtension: nil) != nil
    }

    func start(completion: @escaping (Result<Void, Error>) -> Void) {
        if canStartEmbedded {
            launchPythonEngine()
        } else {
            lastErrorText = EngineError.missingBinary.localizedDescription
        }
        waitForGUI(deadline: Date().addingTimeInterval(25), completion: completion)
    }

    /// Called on first launch to copy the bundled Python project into
    /// Documents/pocketburp, where the embedded Alpine can execute it.
    func seedProjectIfNeeded() {
        let fm = FileManager.default
        guard let src = Bundle.main.url(forResource: "pocketburp", withExtension: nil),
              let docs = fm.urls(for: .documentDirectory, in: .userDomainMask).first else { return }
        let dst = docs.appendingPathComponent("pocketburp")
        guard !fm.fileExists(atPath: dst.path) else { return }
        try? fm.createDirectory(at: dst, withIntermediateDirectories: true)
        if let items = try? fm.contentsOfDirectory(at: src, includingPropertiesForKeys: nil) {
            for item in items {
                try? fm.copyItem(at: item, to: dst.appendingPathComponent(item.lastPathComponent))
            }
        }
    }

    private func launchPythonEngine() {
        seedProjectIfNeeded()
        // Wire the iSH xcframework here, e.g.:
        //   ISHShell.shared.launch(command:
        //       "cd ~/pocketburp && python3 pocketburp.py --host 127.0.0.1")
    }

    private func waitForGUI(deadline: Date, completion: @escaping (Result<Void, Error>) -> Void) {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 2
        config.waitsForConnectivity = false
        let session = URLSession(configuration: config)
        let url = URL(string: "http://127.0.0.1:\(EngineBridge.guiPort)/api/status")!

        func attempt() {
            session.dataTask(with: url) { [weak self] _, response, _ in
                if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                    completion(.success(()))
                    return
                }
                if Date() > deadline {
                    self?.lastErrorText = EngineError.timeout.localizedDescription
                    completion(.failure(EngineError.timeout))
                    return
                }
                DispatchQueue.global().asyncAfter(deadline: .now() + 0.75) { attempt() }
            }.resume()
        }
        attempt()
    }
}
