import Foundation

// Entry point. Two personalities share this one executable:
//
//   OhmyTesla --supervisor      what the LaunchAgent runs (its plist names this
//                               binary as BundleProgram). Replaces itself with the
//                               bundled Python running supervisor.py — before any
//                               AppKit or SwiftUI initialisation happens.
//   OhmyTesla --agent-status | --register-agent | --unregister-agent
//                               headless helpers for scripts and tests.
//   OhmyTesla                   the menu-bar app.

let arguments = Array(CommandLine.arguments.dropFirst())
switch arguments.first {
case "--supervisor":
    SupervisorLauncher.execSupervisor(extraArguments: Array(arguments.dropFirst()))
case "--agent-status", "--register-agent", "--unregister-agent":
    exit(AgentCLI.run(arguments[0]))
default:
    OhmyTeslaApp.main()
}
