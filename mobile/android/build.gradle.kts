allprojects {
    repositories {
        exclusiveContent {
            forRepository { maven { url = uri(rootProject.file("../native/webrtc/android")) } }
            filter { includeGroup("chat.kaede") }
        }
        google()
        mavenCentral()
    }
    configurations.configureEach {
        resolutionStrategy.dependencySubstitution {
            substitute(module("io.github.webrtc-sdk:android"))
                .using(module("chat.kaede:webrtc:137.7151.04-kaede-av1.1"))
                .because("Encrypted AV1 requires Kaede's patched native cryptor")
        }
    }
}

val newBuildDir: Directory =
    rootProject.layout.buildDirectory
        .dir("../../build")
        .get()
rootProject.layout.buildDirectory.value(newBuildDir)

subprojects {
    val newSubprojectBuildDir: Directory = newBuildDir.dir(project.name)
    project.layout.buildDirectory.value(newSubprojectBuildDir)
}
subprojects {
    project.evaluationDependsOn(":app")
}

tasks.register<Delete>("clean") {
    delete(rootProject.layout.buildDirectory)
}
