cmake_minimum_required(VERSION 3.16)
project(onedrivenativepinaction)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(ECM 6.0.0 REQUIRED NO_MODULE)
set(CMAKE_MODULE_PATH ${ECM_MODULE_PATH})

include(KDEInstallDirs)
include(KDECMakeSettings)
include(KDECompilerSettings NO_POLICY_SCOPE)
include(FeatureSummary)

find_package(Qt6 REQUIRED COMPONENTS Core Gui Widgets Network)
find_package(KF6CoreAddons REQUIRED)
find_package(KF6KIO REQUIRED)

kcoreaddons_add_plugin(onedrivenativepinaction
    SOURCES onedrivepinactionplugin.cpp
    INSTALL_NAMESPACE "kf6/kfileitemaction"
)

target_link_libraries(onedrivenativepinaction
    Qt6::Core
    Qt6::Gui
    Qt6::Widgets
    Qt6::Network
    KF6::KIOWidgets
)

feature_summary(WHAT ALL FATAL_ON_MISSING_REQUIRED_PACKAGES)
