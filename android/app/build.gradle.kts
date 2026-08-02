plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.yunbo.scoreplayer"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.yunbo.scoreplayer"
        minSdk = 23
        targetSdk = 35
        versionCode = 15
        versionName = "1.3.3"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin {
    jvmToolchain(17)
}

dependencies {
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
}
