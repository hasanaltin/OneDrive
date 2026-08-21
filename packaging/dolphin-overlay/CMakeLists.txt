cmake_minimum_required(VERSION 3.16)
project(onedrivenativeoverlay)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(ECM 6.0.0 REQUIRED NO_MODULE)
set(CMAKE_MODULE_PATH ${ECM_MODULE_PATH})

include(KDEInstallDirs)
include(KDECMakeSettings)
include(KDECompilerSettings NO_POLICY_SCOPE)
include(FeatureSummary)

find_package(Qt6 REQUIRED COMPONENTS Core Network)
find_package(KF6CoreAddons REQUIRED)
find_package(KF6KIO REQUIRED)

kcoreaddons_add_plugin(onedrivenativeoverlay
    SOURCES onedriveoverlayplugin.cpp
    INSTALL_NAMESPACE "kf6/overlayicon"
)

target_link_libraries(onedrivenativeoverlay
    Qt6::Core
    Qt6::Network
    KF6::KIOCore
)

feature_summary(WHAT ALL FATAL_ON_MISSING_REQUIRED_PACKAGES)
