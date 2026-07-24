-- =========================================
-- TABLE: users
-- =========================================
CREATE TABLE users (
    id SERIAL PRIMARY KEY,

    username VARCHAR(50) UNIQUE NOT NULL,

    name VARCHAR(100) NOT NULL,

    password VARCHAR(255) NOT NULL,

    email VARCHAR(255) UNIQUE NOT NULL,

    supabase_user_id VARCHAR(64) UNIQUE,

    google_id VARCHAR(255) UNIQUE,

    avatar_url VARCHAR(1000),

    provider VARCHAR(32) NOT NULL DEFAULT 'email',

    email_verified_at TIMESTAMPTZ,

    verification_token_hash VARCHAR(64),

    verification_token_expires_at TIMESTAMPTZ,

    reset_token_hash VARCHAR(64),

    reset_token_expires_at TIMESTAMPTZ,

    session_version INTEGER NOT NULL DEFAULT 1,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);


-- =========================================
-- TABLE: conversations
-- =========================================
CREATE TABLE conversations (
    id SERIAL PRIMARY KEY,

    user_id INTEGER NOT NULL,

    title VARCHAR(255),

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_conversation_user
        FOREIGN KEY(user_id)
        REFERENCES users(id)
        ON DELETE CASCADE
);


-- =========================================
-- TABLE: messages
-- =========================================
CREATE TABLE messages (
    id SERIAL PRIMARY KEY,

    conversation_id INTEGER NOT NULL,

    sender VARCHAR(10) NOT NULL
        CHECK (sender IN ('user', 'bot')),

    message TEXT NOT NULL,
    citations JSONB NOT NULL DEFAULT '[]'::jsonb,
    sources JSONB NOT NULL DEFAULT '[]'::jsonb,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_message_conversation
        FOREIGN KEY(conversation_id)
        REFERENCES conversations(id)
        ON DELETE CASCADE
);


-- =========================================
-- INDEX
-- =========================================
CREATE INDEX idx_users_username
ON users(username);

CREATE UNIQUE INDEX ix_users_email_lower
ON users(lower(email));

CREATE UNIQUE INDEX ix_users_google_id
ON users(google_id)
WHERE google_id IS NOT NULL;

CREATE UNIQUE INDEX ix_users_supabase_user_id
ON users(supabase_user_id)
WHERE supabase_user_id IS NOT NULL;

CREATE INDEX idx_conversations_user_id
ON conversations(user_id);

CREATE INDEX idx_messages_conversation_id
ON messages(conversation_id);

CREATE INDEX idx_messages_created_at
ON messages(created_at);

