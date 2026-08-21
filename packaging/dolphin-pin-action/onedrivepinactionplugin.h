#pragma once

#include <KAbstractFileItemActionPlugin>

#include <QList>
#include <QString>
#include <QVariantList>

class QAction;
class QWidget;
class KFileItemListProperties;

// A KAbstractFileItemActionPlugin, loaded directly into Dolphin's (and any
// other KIO-using app's) own process to add "Always keep on this device" /
// "Free up space" to the right-click context menu for folders inside the
// OneDrive on-demand mount - the same underlying pin/unpin state the
// in-app "Choose folders" dialog already exposes as checkboxes, just
// reachable without opening this app's own window first.
//
// Unlike this project's earlier (reverted, see CHANGELOG.md's [0.4.91])
// attempt at a KIO::ThumbnailCreator plugin, this plugin type has no
// competing-plugin problem: KFileItemActions calls actions() on EVERY
// enabled, mimetype-matching plugin and additively combines all their
// results into the context menu (confirmed directly against KDE's own
// kio source, src/widgets/kfileitemactions.cpp) - there is no "only one
// plugin can win" resolution to lose here, so returning an empty list for
// a selection that isn't ours is simply "no OneDrive menu items," never
// "some OTHER plugin's items get suppressed."
//
// Has no access to onedrive-linux-client's own state on its own - every
// call asks the running app over the same local Unix socket the
// overlay-icon plugin already uses (see onedrive/dolphin_overlay_server.py
// for the other end and the exact wire protocol) whether the selected
// path(s) are folders this app tracks, and to actually change the pinned
// flag when an action is triggered.
class OneDrivePinActionPlugin : public KAbstractFileItemActionPlugin
{
    Q_OBJECT

public:
    explicit OneDrivePinActionPlugin(QObject *parent, const QVariantList &args);

    QList<QAction *> actions(const KFileItemListProperties &fileItemInfos, QWidget *parentWidget) override;
};
