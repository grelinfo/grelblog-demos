-- The app's own tables. grelmicro creates its outbox table itself.
-- The family members who get the emails.
CREATE TABLE IF NOT EXISTS family (
    email text PRIMARY KEY
);

-- One plan per week. Finishing it tells the family.
CREATE TABLE IF NOT EXISTS plans (
    week text PRIMARY KEY,
    finished_at timestamptz
);

-- One lunch and one dinner per day: the natural key makes setting it a state.
CREATE TABLE IF NOT EXISTS slots (
    day date NOT NULL,
    meal_type text NOT NULL CHECK (meal_type IN ('lunch', 'dinner')),
    recipe text NOT NULL,
    PRIMARY KEY (day, meal_type)
);

-- Meals added to the week's plan: the same recipe can be added
-- twice on purpose, so no natural key.
CREATE TABLE IF NOT EXISTS meals (
    id uuid PRIMARY KEY,
    week text NOT NULL,
    recipe text NOT NULL
);

-- One email to the family per meal, by outbox message id.
-- sending, then accepted, or bounced if a bounce came back.
CREATE TABLE IF NOT EXISTS emails (
    message_id uuid PRIMARY KEY,
    meal_id uuid NOT NULL,
    status text NOT NULL
);
