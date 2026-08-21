#pragma once

#include <KOverlayIconPlugin>

#include <QObject>
#include <QString>
#include <QStringList>
#include <QUrl>

// A KIO overlay-icon plugin, loaded directly into Dolphin's own process by
// KIO whenever a file view needs to decide what to draw in the corner of
// each icon. It has no access to onedrive-linux-client's own state (a
// separate process, a separate Python program) - every call asks the
// running app over a local Unix socket what it currently knows about that
// exact path, and the app answers from its already-open sync database.
// See onedrive/dolphin_overlay_server.py for the other end of that
// connection and the exact wire protocol.
class OneDriveOverlayPlugin : public KOverlayIconPlugin
{
    Q_OBJECT
    Q_PLUGIN_METADATA(IID "org.kde.overlayicon.onedrivenative")

public:
    explicit OneDriveOverlayPlugin(QObject *parent = nullptr);

    QStringList getOverlays(const QUrl &item) override;

private:
    QString queryStatus(const QString &path) const;
};
