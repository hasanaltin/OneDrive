#include "onedrivepinactionplugin.h"

#include <QAction>
#include <QByteArray>
#include <QClipboard>
#include <QGuiApplication>
#include <QIcon>
#include <QLocalSocket>
#include <QMenu>
#include <QUrl>
#include <QWidget>

#include <KFileItemListProperties>
#include <KPluginFactory>

#include <unistd.h>

namespace
{
// Same tradeoff onedriveoverlayplugin.cpp already makes: a local one-shot
// "connect, ask, disconnect" round trip, simple enough not to need any
// connection-lifecycle handling. This one's queries are always pure local
// DB lookups on the app's side (no Graph API calls, unlike the reverted
// thumbnailer), so the same short timeout as the overlay plugin is fine.
constexpr int kTimeoutMs = 2000;

QString socketPath()
{
    const QByteArray runtimeDir = qgetenv("XDG_RUNTIME_DIR");
    const QString base = runtimeDir.isEmpty() ? QStringLiteral("/run/user/%1").arg(getuid())
                                               : QString::fromLocal8Bit(runtimeDir);
    return base + QStringLiteral("/OneDrive-overlay.sock");
}

QString sendRequest(const QString &request)
{
    QLocalSocket socket;
    socket.connectToServer(socketPath());
    if (!socket.waitForConnected(kTimeoutMs)) {
        return QString();
    }
    socket.write((request + QLatin1Char('\n')).toUtf8());
    if (!socket.waitForBytesWritten(kTimeoutMs) || !socket.waitForReadyRead(kTimeoutMs)) {
        return QString();
    }
    return QString::fromUtf8(socket.readAll()).trimmed();
}

QString pinState(const QString &path)
{
    return sendRequest(QStringLiteral("PINSTATE %1").arg(path));
}

void setPin(const QString &path, bool pinned)
{
    sendRequest(QStringLiteral("SETPIN %1 %2").arg(pinned ? QStringLiteral("1") : QStringLiteral("0"), path));
}

// Cheap, purely-local (no Graph API call) check reused from the overlay-icon
// protocol just to decide whether a path is something the app tracks at all
// (mount OR Folder Pair) - used only to decide whether "Copy OneDrive Link"/
// "Manage OneDrive Access" should appear in the menu. The actual SHARELINK/
// MANAGEACCESS requests (which DO call Graph) only ever fire from the
// action's own triggered handler, i.e. only when the user actually clicks
// it - never just from opening the context menu.
bool isTrackedPath(const QString &path)
{
    return sendRequest(QStringLiteral("STATUS %1").arg(path)) != QStringLiteral("NONE");
}

QString shareLink(const QString &path)
{
    return sendRequest(QStringLiteral("SHARELINK %1").arg(path));
}

void requestManageAccess(const QString &path)
{
    sendRequest(QStringLiteral("MANAGEACCESS %1").arg(path));
}

void requestOpenShare(const QString &path)
{
    sendRequest(QStringLiteral("OPENSHARE %1").arg(path));
}

// Local (not is_folder-only cloud placeholder) paths from the selection
// that this app actually tracks as a pinnable OneDrive folder - anything
// outside the mount, or a plain file, comes back PINSTATE NONE and is
// simply excluded rather than blocking the whole selection.
QStringList trackedFolderPaths(const KFileItemListProperties &fileItemInfos)
{
    QStringList paths;
    const auto urls = fileItemInfos.urlList();
    for (const QUrl &url : urls) {
        if (!url.isLocalFile()) {
            continue;
        }
        const QString path = url.toLocalFile();
        if (pinState(path) != QStringLiteral("NONE")) {
            paths << path;
        }
    }
    return paths;
}
}

OneDrivePinActionPlugin::OneDrivePinActionPlugin(QObject *parent, const QVariantList &args)
    : KAbstractFileItemActionPlugin(parent)
{
    Q_UNUSED(args)
}

// All of this plugin's actions get grouped under a single top-level
// "OneDrive" entry rather than appearing loose in the context menu -
// KAbstractFileItemActionPlugin/KFileItemActions itself has no notion of a
// plugin-named submenu (its own X-KDE-Show-In-Submenu metadata flag only
// ever routes into KDE's single shared, generic "Actions" catch-all -
// confirmed directly from kio's own kfileitemactions.cpp source rather
// than assumed), so this uses the same plain, standard Qt technique any
// app uses for a submenu: a single QAction with QAction::setMenu() set,
// containing the real entries. Dolphin/KFileItemActions still just sees
// one QAction from us either way - the additive-combination safety
// property from the class docs above is unaffected.
QList<QAction *> OneDrivePinActionPlugin::actions(const KFileItemListProperties &fileItemInfos, QWidget *parentWidget)
{
    const QStringList pinPaths = trackedFolderPaths(fileItemInfos);

    const auto urls = fileItemInfos.urlList();
    const bool singleTrackedSelection = urls.size() == 1 && urls.first().isLocalFile()
        && isTrackedPath(urls.first().toLocalFile());

    if (pinPaths.isEmpty() && !singleTrackedSelection) {
        return {};
    }

    auto *submenu = new QMenu(parentWidget);

    if (!pinPaths.isEmpty()) {
        auto *keepAction = new QAction(QIcon::fromTheme(QStringLiteral("emblem-downloads")),
                                        QObject::tr("Always keep on this device"), submenu);
        QObject::connect(keepAction, &QAction::triggered, keepAction, [pinPaths]() {
            for (const QString &path : pinPaths) {
                setPin(path, true);
            }
        });
        submenu->addAction(keepAction);

        auto *freeAction = new QAction(QIcon::fromTheme(QStringLiteral("edit-delete")),
                                        QObject::tr("Free up space (OneDrive)"), submenu);
        QObject::connect(freeAction, &QAction::triggered, freeAction, [pinPaths]() {
            for (const QString &path : pinPaths) {
                setPin(path, false);
            }
        });
        submenu->addAction(freeAction);
    }

    // Share / Copy link / Manage access: single-item selections only (v1 -
    // matches how most cloud clients only offer these for one selected
    // item), applies to both files and folders, tracked via either the
    // on-demand mount or a Folder Pair. Naming deliberately mirrors the
    // OneDrive/SharePoint web UI's own "Share"/"Copy link"/"Manage access"
    // labels exactly, rather than repeating "OneDrive" in each one - the
    // parent submenu they're all under already provides that context.
    if (singleTrackedSelection) {
        if (!pinPaths.isEmpty()) {
            submenu->addSeparator();
        }
        const QString path = urls.first().toLocalFile();

        auto *shareAction = new QAction(QIcon::fromTheme(QStringLiteral("document-share")),
                                         QObject::tr("Share…"), submenu);
        QObject::connect(shareAction, &QAction::triggered, shareAction, [path]() {
            requestOpenShare(path);
        });
        submenu->addAction(shareAction);

        auto *linkAction = new QAction(QIcon::fromTheme(QStringLiteral("link")),
                                        QObject::tr("Copy link"), submenu);
        QObject::connect(linkAction, &QAction::triggered, linkAction, [path]() {
            const QString response = shareLink(path);
            if (response.startsWith(QStringLiteral("LINK "))) {
                QGuiApplication::clipboard()->setText(response.mid(5));
            }
        });
        submenu->addAction(linkAction);

        auto *manageAction = new QAction(QIcon::fromTheme(QStringLiteral("system-users")),
                                          QObject::tr("Manage access"), submenu);
        QObject::connect(manageAction, &QAction::triggered, manageAction, [path]() {
            requestManageAccess(path);
        });
        submenu->addAction(manageAction);
    }

    auto *rootAction = new QAction(
        QIcon::fromTheme(QStringLiteral("folder-onedrive"), QIcon::fromTheme(QStringLiteral("folder-cloud"))),
        QObject::tr("OneDrive"), parentWidget);
    rootAction->setMenu(submenu);
    return {rootAction};
}

K_PLUGIN_CLASS_WITH_JSON(OneDrivePinActionPlugin, "onedrivepinaction.json")

#include "onedrivepinactionplugin.moc"
