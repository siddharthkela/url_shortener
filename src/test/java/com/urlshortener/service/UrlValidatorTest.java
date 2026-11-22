package com.urlshortener.service;

import com.urlshortener.exception.InvalidUrlException;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.time.temporal.ChronoUnit;

import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class UrlValidatorTest {

    @Test
    void acceptsHttpAndHttpsUrls() {
        assertThatCode(() -> UrlValidator.validateOriginalUrl("http://example.com")).doesNotThrowAnyException();
        assertThatCode(() -> UrlValidator.validateOriginalUrl("https://example.com/a/b?c=d")).doesNotThrowAnyException();
    }

    @Test
    void schemeCheckIsCaseInsensitive() {
        assertThatCode(() -> UrlValidator.validateOriginalUrl("HTTPS://example.com")).doesNotThrowAnyException();
    }

    @Test
    void rejectsNonHttpSchemes() {
        assertThatThrownBy(() -> UrlValidator.validateOriginalUrl("ftp://example.com"))
                .isInstanceOf(InvalidUrlException.class)
                .hasMessageContaining("http or https");
        assertThatThrownBy(() -> UrlValidator.validateOriginalUrl("javascript:alert(1)"))
                .isInstanceOf(InvalidUrlException.class);
    }

    @Test
    void rejectsUrlWithoutScheme() {
        assertThatThrownBy(() -> UrlValidator.validateOriginalUrl("example.com/page"))
                .isInstanceOf(InvalidUrlException.class);
    }

    @Test
    void rejectsSyntacticallyMalformedUrl() {
        assertThatThrownBy(() -> UrlValidator.validateOriginalUrl("http://exa mple.com"))
                .isInstanceOf(InvalidUrlException.class)
                .hasMessageContaining("Malformed URL");
    }

    @Test
    void rejectsUrlWithoutHost() {
        assertThatThrownBy(() -> UrlValidator.validateOriginalUrl("http:///path-only"))
                .isInstanceOf(InvalidUrlException.class)
                .hasMessageContaining("Malformed URL");
    }

    @Test
    void nullExpiryMeansNeverExpiresAndIsAllowed() {
        assertThatCode(() -> UrlValidator.validateExpiresAt(null)).doesNotThrowAnyException();
    }

    @Test
    void futureExpiryIsAllowed() {
        assertThatCode(() -> UrlValidator.validateExpiresAt(Instant.now().plus(1, ChronoUnit.DAYS)))
                .doesNotThrowAnyException();
    }

    @Test
    void pastExpiryIsRejected() {
        assertThatThrownBy(() -> UrlValidator.validateExpiresAt(Instant.now().minus(1, ChronoUnit.MINUTES)))
                .isInstanceOf(InvalidUrlException.class)
                .hasMessage("expiresAt must be in the future");
    }
}
