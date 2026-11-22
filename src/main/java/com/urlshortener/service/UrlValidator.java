package com.urlshortener.service;

import com.urlshortener.exception.InvalidUrlException;

import java.net.URI;
import java.net.URISyntaxException;
import java.time.Instant;

/**
 * Stateless input validation for short-URL requests, extracted from
 * UrlService so it can be tested on its own. Static utility, matching the
 * UrlMapper and Base62Encoder pattern used elsewhere in this codebase.
 */
public final class UrlValidator {

    private UrlValidator() {
    }

    public static void validateOriginalUrl(String url) {
        URI uri;
        try {
            uri = new URI(url);
        } catch (URISyntaxException e) {
            throw new InvalidUrlException("Malformed URL: " + url);
        }
        String scheme = uri.getScheme();
        if (scheme == null || !(scheme.equalsIgnoreCase("http") || scheme.equalsIgnoreCase("https"))) {
            throw new InvalidUrlException("URL must use http or https scheme: " + url);
        }
        if (uri.getHost() == null) {
            throw new InvalidUrlException("Malformed URL: " + url);
        }
    }

    public static void validateExpiresAt(Instant expiresAt) {
        if (expiresAt != null && expiresAt.isBefore(Instant.now())) {
            throw new InvalidUrlException("expiresAt must be in the future");
        }
    }
}
