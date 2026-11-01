// Renders the OhmyTesla app icon (the hub's static/icon.svg: a red bolt on a
// white rounded square) into an .iconset, at macOS icon-grid proportions.
//   swiftc -O make_icon.swift -o make_icon && ./make_icon AppIcon.iconset
import AppKit

let out = URL(fileURLWithPath: CommandLine.arguments.dropFirst().first ?? "AppIcon.iconset")
try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

func render(_ px: Int) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                               colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let ctx = NSGraphicsContext.current!.cgContext
    let s = CGFloat(px) / 1024
    // Big Sur grid: 824x824 body, centred, corner radius ~185
    let body = CGRect(x: 100 * s, y: 100 * s, width: 824 * s, height: 824 * s)
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -10 * s), blur: 28 * s,
                  color: NSColor.black.withAlphaComponent(0.22).cgColor)
    let shape = CGPath(roundedRect: body, cornerWidth: 185 * s, cornerHeight: 185 * s, transform: nil)
    ctx.addPath(shape)
    ctx.setFillColor(NSColor.white.cgColor)
    ctx.fillPath()
    ctx.restoreGState()
    // A faint top-to-bottom shade and a hairline edge, so the white body reads on light docks.
    ctx.saveGState()
    ctx.addPath(shape)
    ctx.clip()
    let shade = CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB),
                           colors: [NSColor(white: 1, alpha: 1).cgColor, NSColor(srgbRed: 0.94, green: 0.945, blue: 0.955, alpha: 1).cgColor] as CFArray,
                           locations: [0, 1])!
    ctx.drawLinearGradient(shade, start: CGPoint(x: 0, y: body.maxY), end: CGPoint(x: 0, y: body.minY), options: [])
    ctx.restoreGState()
    ctx.addPath(CGPath(roundedRect: body.insetBy(dx: 1.5 * s, dy: 1.5 * s), cornerWidth: 183.5 * s, cornerHeight: 183.5 * s, transform: nil))
    ctx.setStrokeColor(NSColor(srgbRed: 0.886, green: 0.894, blue: 0.91, alpha: 1).cgColor)
    ctx.setLineWidth(3 * s)
    ctx.strokePath()
    // The bolt from icon.svg (512-unit, y down) mapped into the body.
    let pts: [(CGFloat, CGFloat)] = [(256, 96), (168, 288), (232, 288), (208, 416), (344, 224), (280, 224), (304, 96)]
    let k = 824 * s / 512
    let path = CGMutablePath()
    for (i, p) in pts.enumerated() {
        let pt = CGPoint(x: body.minX + p.0 * k, y: body.maxY - p.1 * k)
        if i == 0 { path.move(to: pt) } else { path.addLine(to: pt) }
    }
    path.closeSubpath()
    ctx.addPath(path)
    ctx.setFillColor(NSColor(srgbRed: 0xe3 / 255, green: 0x19 / 255, blue: 0x37 / 255, alpha: 1).cgColor)
    ctx.fillPath()
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

for base in [16, 32, 128, 256, 512] {
    try! render(base).write(to: out.appendingPathComponent("icon_\(base)x\(base).png"))
    try! render(base * 2).write(to: out.appendingPathComponent("icon_\(base)x\(base)@2x.png"))
}
