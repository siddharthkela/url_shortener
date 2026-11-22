package com.urlshortener.integration;

import com.urlshortener.dto.CreateUrlRequest;
import com.urlshortener.dto.UrlResponse;
import com.urlshortener.service.UrlService;
import com.urlshortener.util.Base62Encoder;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Regression test for a real collision bug: custom aliases and generated
 * Base62 codes share one namespace. If a custom alias equals Base62(id) of a
 * row created later, that generated create used to fail with a unique
 * constraint violation (returned to the client as 409 Conflict on a request
 * that never asked for an alias).
 */
@SpringBootTest
class ShortCodeCollisionRegressionTest {

    @Autowired
    private UrlService urlService;

    @Autowired
    private JdbcTemplate jdbc;

    @Test
    void generatedCodeNeverCollidesWithAnExistingCustomAlias() {
        // Move the identity sequence to a range where Base62(id) is a valid
        // 3+ character alias, so the collision can be set up deterministically.
        Long maxId = jdbc.queryForObject("SELECT COALESCE(MAX(id), 0) FROM short_urls", Long.class);
        long start = Math.max(maxId + 1, 200_000L);
        jdbc.execute("ALTER TABLE short_urls ALTER COLUMN id RESTART WITH " + start);

        // The alias row takes id `start`; the next generated row gets id
        // `start + 1`, whose Base62 encoding is exactly this alias.
        String alias = Base62Encoder.encode(start + 1);
        urlService.createShortUrl(new CreateUrlRequest("https://example.com/alias-target", alias, null), null);

        UrlResponse generated = urlService.createShortUrl(
                new CreateUrlRequest("https://example.com/generated-target", null, null), null);

        assertThat(generated.shortCode()).isNotEqualTo(alias);
        assertThat(urlService.resolve(alias)).isEqualTo("https://example.com/alias-target");
        assertThat(urlService.resolve(generated.shortCode())).isEqualTo("https://example.com/generated-target");
    }
}
