import java.io.FileInputStream
import java.util.Properties

plugins {
    id("com.android.application")
    id("kotlin-android")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

// Firebase remains optional for self-hosted builds. When release automation
// injects google-services.json, apply the provider plugin automatically.
if (file("google-services.json").exists()) {
    apply(plugin = "com.google.gms.google-services")
}

val keystoreProperties = Properties()
val keystorePropertiesFile = rootProject.file("key.properties")
if (keystorePropertiesFile.exists()) {
    FileInputStream(keystorePropertiesFile).use(keystoreProperties::load)
}

android {
    namespace = "chat.kaede.kaede_mobile"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    compileOptions {
        isCoreLibraryDesugaringEnabled = true
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = JavaVersion.VERSION_17.toString()
    }

    defaultConfig {
        applicationId = "chat.kaede.mobile"
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    signingConfigs {
        if (keystorePropertiesFile.exists()) {
            create("release") {
                keyAlias = keystoreProperties.getProperty("keyAlias")
                keyPassword = keystoreProperties.getProperty("keyPassword")
                storeFile = rootProject.file(keystoreProperties.getProperty("storeFile"))
                storePassword = keystoreProperties.getProperty("storePassword")
            }
        }
    }

    buildTypes {
        release {
            signingConfig = signingConfigs.findByName("release")
        }
    }
}

flutter {
    source = "../.."
}

dependencies {
    coreLibraryDesugaring("com.android.tools:desugar_jdk_libs:2.1.5")
}

val buildKaedeE2ee by tasks.registering(Exec::class) {
    val nativeScript = rootProject.file("../tool/build_e2ee_native.sh")
    val rustSources = rootProject.file("../../desktop/crates/kaede-e2ee/src")
    inputs.dir(rustSources)
    inputs.dir(rootProject.file("../../desktop/crates/kaede-e2ee-ffi/src"))
    inputs.file(rootProject.file("../../desktop/Cargo.toml"))
    inputs.file(rootProject.file("../../desktop/crates/kaede-e2ee/Cargo.toml"))
    inputs.file(rootProject.file("../../desktop/crates/kaede-e2ee-ffi/Cargo.toml"))
    inputs.file(rootProject.file("../../desktop/Cargo.lock"))
    outputs.dir(project.file("src/main/jniLibs"))
    environment("ANDROID_HOME", android.sdkDirectory.absolutePath)
    environment("KAEDE_ANDROID_NDK_VERSION", android.ndkVersion)
    environment("KAEDE_ANDROID_MIN_SDK", android.defaultConfig.minSdk ?: 23)
    commandLine("bash", nativeScript.absolutePath, "--android")
}

tasks.named("preBuild").configure {
    dependsOn(buildKaedeE2ee)
}
