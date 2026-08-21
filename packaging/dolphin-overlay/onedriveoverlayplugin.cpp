#include "onedriveoverlayplugin.h"

#include <QByteArray>
#include <QLocalSocket>

#include <unistd.h>

namespace
{
// A local one-shot request/response - "connect, ask, disconnect" - is the
// same tradeoff onedrive/dolphin_overlay_server.py's docstring explains:
// simple enough not to need any connection-lifecycle handling, and cheap
// enough (a local Unix socket round trip) that Dolphin asking about every
// visible file on every folder open is not something worth optimizing away
// for a first version. Short timeouts throughout so a slow/dead server
// (app not running, or momentarily busy) never makes a Dolphin folder
// listing visibly hang - a missing overlay icon is a much smaller problem
// than a frozen file manager, and getOverlays() is documented as being
// called from the main thread and must not block.
constexpr int kTimeoutMs = 200;

QString socketPath()
{
    const QByteArray runtimeDir = qgetenv("XDG_RUNTIME_DIR");
    const QString base = runtimeDir.isEmpty() ? QStringLiteral("/run/user/%1").arg(getuid())
                                               : QString::fromLocal8Bit(runtimeDir);
    return base + QStringLiteral("/OneDrive-overlay.sock");
}
}

OneDriveOverlayPlugin::OneDriveOverlayPlugin(QObject *parent)
    : KOverlayIconPlugin(parent)
{
}

QString OneDriveOverlayPlugin::queryStatus(const QString &path) const
{
    QLocalSocket socket;
    socket.connectToServer(socketPath());
    if (!socket.waitForConnected(kTimeoutMs)) {
        return QString();
    }

    const QByteArray request = QStringLiteral("STATUS %1\n").arg(path).toUtf8();
    socket.write(request);
    if (!socket.waitForBytesWritten(kTimeoutMs)) {
        return QString();
    }
    if (!socket.waitForReadyRead(kTimeoutMs)) {
        return QString();
    }
    return QString::fromUtf8(socket.readAll()).trimmed();
}

QStringList OneDriveOverlayPlugin::getOverlays(const QUrl &item)
{
    if (!item.isLocalFile()) {
        return {};
    }
    const QString status = queryStatus(item.toLocalFile());
    if (status == QStringLiteral("LOCAL")) {
        return {QStringLiteral("onedrive-status-local")};
    }
    if (status == QStringLiteral("CLOUD")) {
        return {QStringLiteral("onedrive-status-cloud")};
    }
    if (status == QStringLiteral("SYNCING")) {
        return {QStringLiteral("onedrive-status-syncing")};
    }
    // "NONE", an empty response (server not running / timed out), or
    // anything unrecognized - no opinion, draw nothing.
    return {};
}
