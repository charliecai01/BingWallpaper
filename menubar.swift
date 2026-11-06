// Menu bar button: each click runs `bing_wallpaper.py --rotate` right away,
// with no half-day limit. The tooltip shows the result of the last click.
//
// Built and launched by auto_menubar.sh as: .menubar <repo dir> <python3 path>

import AppKit

final class RotateButton: NSObject {
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    private let repoDir: String
    private let python: String

    init(repoDir: String, python: String) {
        self.repoDir = repoDir
        self.python = python
        super.init()
        guard let button = item.button else { return }
        button.image = icon("photo.on.rectangle")
        button.toolTip = "Rotate wallpaper"
        button.target = self
        button.action = #selector(rotate)
    }

    private func icon(_ name: String) -> NSImage? {
        NSImage(systemSymbolName: name, accessibilityDescription: "Rotate wallpaper")
    }

    @objc private func rotate() {
        item.button?.isEnabled = false

        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = ["bing_wallpaper.py", "--rotate"]
        process.currentDirectoryURL = URL(fileURLWithPath: repoDir)
        process.standardOutput = pipe
        process.standardError = pipe
        process.terminationHandler = { [weak self] finished in
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            let output = String(decoding: data, as: UTF8.self)
            DispatchQueue.main.async {
                self?.finish(output: output, succeeded: finished.terminationStatus == 0)
            }
        }

        do {
            try process.run()
        } catch {
            finish(output: "\(error)", succeeded: false)
        }
    }

    private func finish(output: String, succeeded: Bool) {
        let lastLine = output.split(whereSeparator: \.isNewline).last.map(String.init) ?? ""
        print(lastLine)
        fflush(stdout)
        item.button?.isEnabled = true
        item.button?.image = icon(succeeded ? "photo.on.rectangle" : "exclamationmark.triangle")
        item.button?.toolTip = lastLine
    }
}

guard CommandLine.arguments.count == 3 else {
    print("usage: \(CommandLine.arguments[0]) <repo dir> <python3 path>")
    exit(1)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let rotateButton = RotateButton(repoDir: CommandLine.arguments[1], python: CommandLine.arguments[2])
app.run()
