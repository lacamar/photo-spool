#include "shim.h"

#include <QtCore/QBuffer>
#include <QtGui/QIcon>
#include <QtGui/QImage>
#include <QtQuick/QQuickImageProvider>
#include <QtWidgets/QApplication>
#include <QtCore/QTimer>

#include "photo-spool/src/qt/shim.cxxqt.h"

namespace {

// QApplication, not QGuiApplication: QtQuick.Dialogs may need QtWidgets for the folder dialog
int argc = 1;
char arg0[] = "photo-spool";
char* argv[] = { arg0, nullptr };
QApplication* app = nullptr;

constexpr int CACHE_MAX_DIMENSION = 320;
constexpr int CACHE_JPEG_QUALITY = 85;

class ThumbnailProvider : public QQuickImageProvider
{
public:
  ThumbnailProvider()
    : QQuickImageProvider(QQuickImageProvider::Image)
  {
  }

  QImage requestImage(const QString& id,
                      QSize* size,
                      const QSize& requested) override
  {
    const QByteArray key = id.toUtf8();
    const rust::Str rid(key.constData(), key.size());
    bool cached = false;
    const rust::Vec<uint8_t> raw = thumb_load(rid, cached);
    if (raw.empty())
      return {};
    const QByteArray bytes(reinterpret_cast<const char*>(raw.data()),
                           static_cast<qsizetype>(raw.size()));
    QImage image = QImage::fromData(bytes);
    if (image.isNull())
      return {};
    if (!cached) {
      // shrink before caching: raw previews can be near full resolution
      QByteArray stored = bytes;
      if (image.width() > CACHE_MAX_DIMENSION ||
          image.height() > CACHE_MAX_DIMENSION) {
        image = image.scaled(CACHE_MAX_DIMENSION,
                             CACHE_MAX_DIMENSION,
                             Qt::KeepAspectRatio,
                             Qt::SmoothTransformation);
        QBuffer buffer;
        buffer.open(QIODevice::WriteOnly);
        if (image.save(&buffer, "JPEG", CACHE_JPEG_QUALITY))
          stored = buffer.data();
      }
      thumb_store(rid,
                  rust::Slice<const uint8_t>(
                    reinterpret_cast<const uint8_t*>(stored.constData()),
                    static_cast<size_t>(stored.size())));
    }
    if (requested.isValid() && requested.width() > 0 &&
        requested.height() > 0)
      image = image.scaled(
        requested, Qt::KeepAspectRatio, Qt::SmoothTransformation);
    if (size)
      *size = image.size();
    return image;
  }
};

}

void
app_create()
{
  app = new QApplication(argc, argv);
  QApplication::setApplicationName(QStringLiteral("Photo Spool"));
  QApplication::setOrganizationName(QStringLiteral("photo-spool"));
  // closing the window hides it; only the tray quits
  QApplication::setQuitOnLastWindowClosed(false);
  QApplication::setDesktopFileName(QStringLiteral("photo-spool"));
  QApplication::setWindowIcon(QIcon::fromTheme(QStringLiteral("photo-spool")));
}

int
app_exec()
{
  return QApplication::exec();
}

void
app_quit()
{
  QMetaObject::invokeMethod(
    QCoreApplication::instance(), &QCoreApplication::quit, Qt::QueuedConnection);
}

void
app_destroy()
{
  delete app;
  app = nullptr;
}

bool
load_main(QQmlApplicationEngine& engine)
{
  engine.addImageProvider(QStringLiteral("thumb"), new ThumbnailProvider);
  engine.load(QUrl(QStringLiteral("qrc:/qt/qml/PhotoSpool/qml/Main.qml")));
  return !engine.rootObjects().isEmpty();
}
