BEGIN;

CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL, 
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

-- Running upgrade  -> a1b2c3d4e5f6

CREATE OR REPLACE FUNCTION fn_set_updated_at()
        RETURNS TRIGGER
        LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$;;

CREATE TABLE guild_config (
            guild_id   BIGINT       PRIMARY KEY,
            prefix     VARCHAR(10)  NOT NULL DEFAULT 't!',
            created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        );;

CREATE TRIGGER trg_guild_config_updated_at
        BEFORE UPDATE ON guild_config
        FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();;

CREATE TABLE blacklisted_users (
            user_id        BIGINT       PRIMARY KEY,
            guild_id       BIGINT       NOT NULL,
            user_name      VARCHAR(100) NOT NULL,
            blacklisted_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        );;

CREATE INDEX idx_blacklisted_users_guild
            ON blacklisted_users (guild_id);;

CREATE TABLE afk_status (
            user_id       BIGINT       PRIMARY KEY,
            guild_id      BIGINT       NOT NULL,
            original_name VARCHAR(100) NOT NULL,
            reason        TEXT         NOT NULL DEFAULT 'No reason provided',
            created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        );;

CREATE INDEX idx_afk_status_guild
            ON afk_status (guild_id);;

CREATE TABLE bot_votes (
            id       TEXT    PRIMARY KEY,
            likes    INTEGER NOT NULL DEFAULT 0,
            dislikes INTEGER NOT NULL DEFAULT 0,
            CONSTRAINT chk_bot_votes_non_negative
                CHECK (likes >= 0 AND dislikes >= 0)
        );;

CREATE TABLE vote_records (
            user_id  BIGINT      PRIMARY KEY,
            voted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );;

CREATE TABLE suggestion_settings (
            guild_id           BIGINT  PRIMARY KEY,
            channel_id         BIGINT,
            approve_channel_id BIGINT,
            deny_channel_id    BIGINT,
            suggestion_count   INTEGER NOT NULL DEFAULT 1,
            CONSTRAINT chk_suggestion_count_positive
                CHECK (suggestion_count >= 1)
        );;

CREATE TABLE suggestions (
            message_id   BIGINT      PRIMARY KEY,
            guild_id     BIGINT,
            suggestor_id BIGINT      NOT NULL,
            serial_no    INTEGER     NOT NULL,
            suggestion   TEXT        NOT NULL,
            is_reviewed  VARCHAR(10) DEFAULT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            reviewed_at  TIMESTAMPTZ,
            CONSTRAINT chk_suggestions_is_reviewed
                CHECK (is_reviewed IS NULL
                    OR is_reviewed IN ('accepted', 'rejected'))
        );;

CREATE INDEX idx_suggestions_suggestor
            ON suggestions (suggestor_id);;

CREATE INDEX idx_suggestions_guild
            ON suggestions (guild_id)
            WHERE guild_id IS NOT NULL;;

CREATE TABLE invite_tracking (
            id            BIGSERIAL   PRIMARY KEY,
            guild_id      BIGINT      NOT NULL,
            inviter_id    BIGINT      NOT NULL,
            count         INTEGER     NOT NULL DEFAULT 0,
            invited_users BIGINT[]    NOT NULL DEFAULT '{}',
            log_channel   BIGINT,
            CONSTRAINT uq_invite_tracking_guild_inviter
                UNIQUE (guild_id, inviter_id),
            CONSTRAINT chk_invite_count_non_negative
                CHECK (count >= 0)
        );;

CREATE INDEX idx_invite_tracking_guild
            ON invite_tracking (guild_id);;

INSERT INTO alembic_version (version_num) VALUES ('a1b2c3d4e5f6') RETURNING alembic_version.version_num;

-- Running upgrade a1b2c3d4e5f6 -> 720720aeef03

UPDATE alembic_version SET version_num='720720aeef03' WHERE alembic_version.version_num = 'a1b2c3d4e5f6';

COMMIT;

