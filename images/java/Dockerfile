# Java 21 (Temurin JDK) with Maven 3.9.x and Gradle 8.x.
FROM eclipse-temurin:21-jdk-jammy

ENV MAVEN_VERSION=3.9.9 \
    GRADLE_VERSION=8.10.2 \
    MAVEN_HOME=/opt/maven \
    GRADLE_HOME=/opt/gradle
ENV PATH="${MAVEN_HOME}/bin:${GRADLE_HOME}/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    unzip \
    ca-certificates \
    && curl -fsSL "https://archive.apache.org/dist/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz" \
       -o /tmp/maven.tar.gz \
    && tar -xzf /tmp/maven.tar.gz -C /opt \
    && mv "/opt/apache-maven-${MAVEN_VERSION}" "${MAVEN_HOME}" \
    && curl -fsSL "https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip" \
       -o /tmp/gradle.zip \
    && unzip -q /tmp/gradle.zip -d /opt \
    && mv "/opt/gradle-${GRADLE_VERSION}" "${GRADLE_HOME}" \
    && rm -f /tmp/maven.tar.gz /tmp/gradle.zip \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
