# Spec: Lost, doubled, unseen

The contract for the three posts. Every claim a post makes is a rule here, and every rule has a test (`tests/`, the test name ends with the rule id). A claim without a rule does not go in a post. A rule without a test is not done.

The demo is a small family meal planner, generic and written from scratch: a weekly plan with lunch and dinner slots, meals added to the week's plan, and an email to the family when someone finishes the week's plan. Email goes to a stub SMTP server that can be made slow, down, or lose its answer. Bounces are read from a real IMAP mailbox (GreenMail in Podman). Nothing is copied from Loïc's real app.

Stack: FastAPI, grelmicro (`Outbox`, `Tasks`, `PostgresProvider`), asyncpg, Postgres 17 in Podman for every test that claims a database behavior.

## Part 1: Why can FastAPI BackgroundTasks lose emails?

Step files: `steps/1_inline.py` (sending inside the request), `steps/2_background_tasks.py` (before), `steps/3_outbox.py` (after).

| Id | Rule | Test |
|---|---|---|
| P1-R01 | With a slow SMTP server, sending inside the request makes finishing the plan wait for SMTP, once per family member, one after the other. | `test_inline_send_waits_for_smtp_P1_R01` |
| P1-R02 | With `BackgroundTasks`, finishing the plan answers 200 before the email is sent. | `test_background_task_answers_before_sending_P1_R02` |
| P1-R03 | With `BackgroundTasks`, stopping the process after the 200 and before the send loses the email: no email, no record of it. | `test_background_task_lost_on_stop_P1_R03` |
| P1-R04 | With the outbox, the finished plan and the email message commit together. A rollback leaves neither. | `test_rollback_leaves_no_email_P1_R04` |
| P1-R05 | With the outbox, stopping the process after the 200 and before the send does not lose the email: the next process sends it. | `test_outbox_survives_stop_P1_R05` |
| P1-R06 | With the outbox, an SMTP outage is retried with backoff until SMTP is back. | `test_outbox_retries_until_smtp_is_back_P1_R06` |
| P1-R07 | After the configured attempts, a message moves to the dead-letter state and stops blocking the others. | `test_dead_letter_after_max_attempts_P1_R07` |
| P1-R08 | Delivery is at least once: if SMTP took the mail and the process dies before the message is marked done, the next process sends it again. | `test_crash_after_send_sends_twice_P1_R08` |
| P1-R12 | By default, a delivered message is deleted from the outbox. | `test_delivered_message_is_deleted_P1_R12` |
| P1-R13 | Finishing a plan stages one message per family member, in the same transaction. A recipient that fails is retried alone: the others are sent once. | `test_one_message_per_person_P1_R13` |
| P1-R14 | Finishing a plan that is already finished queues no new emails, sent again in sequence or five at once. | `test_finishing_twice_sends_once_P1_R14` |
| P1-R15 | With `BackgroundTasks`, the sends run one after the other: a failed send skips the ones after it. | `test_background_failure_skips_the_rest_P1_R15` |
| P1-R10 | A graceful stop (SIGTERM) waits for a running background task, so the email is sent. A stop that times out, as a deploy does when SIGTERM takes too long, kills the process and loses the email. | `test_graceful_stop_waits_but_a_stop_timeout_loses_it_P1_R10` |
| P1-R11 | With the outbox, a stop that times out loses nothing: the next process sends the email. | `test_outbox_survives_a_stop_timeout_P1_R11` |
| P1-R09 | The message stores the intent (template and parameters), not the rendered email. | `test_message_stores_intent_not_body_P1_R09` |

## Part 2: Why can a FastAPI retry add a meal twice?

Step file: `steps/4_add_meal.py`.

| Id | Rule | Test |
|---|---|---|
| P2-R01 | Setting a slot (date, lunch or dinner) twice leaves one slot: a natural key makes it a state. | `test_setting_a_slot_twice_keeps_one_P2_R01` |
| P2-R02 | A plain `POST` that adds a meal to the week's plan, sent twice, adds it twice. | `test_plain_post_adds_twice_P2_R02` |
| P2-R03 | With a client-generated id, `PUT /plans/{week}/meals/{id}` sent twice adds the meal once and answers the same both times. | `test_put_with_client_id_adds_once_P2_R03` |
| P2-R04 | The same id sent from two connections at the same time adds the meal once. | `test_same_id_at_once_adds_once_P2_R04` |
| P2-R05 | The same id with a different meal is refused, not silently replaced. | `test_same_id_other_meal_refused_P2_R05` |
| P2-R08 | The same `Idempotency-Key` with another meal is refused with a 409, not replayed. | `test_same_key_other_meal_refused_P2_R08` |
| P2-R07 | An `Idempotency-Key` with grelmicro `Idempotency` replays the stored answer for a repeated key. But the stored answer and the database write are separate: a failure between them runs the work again and adds a second row. | `test_key_and_write_are_separate_P2_R07` |

The client id needs no rule of its own for the crash case: the id is the row, so there is nothing to store elsewhere and nothing to come apart. The post states it as reasoning.

## Part 3: What if the mail server rejects the email?

Step file: `steps/5_delivery.py`.

Plain SMTP everywhere, no provider API or webhook. SMTP reports failures only, as bounce emails to the envelope sender. So the statuses are sending, accepted and bounced. There is no "delivered".

| Id | Rule | Test |
|---|---|---|
| P3-R01 | Adding a meal queues one family email in the same transaction, with the meal id as `dedup_key`. A retried add queues nothing more, at once or after the send. | `test_retried_add_queues_one_email_P3_R01` |
| P3-R02 | Without `keep_delivered`, the `dedup_key` stops protecting once the message is delivered. With a window, a late retry still queues nothing. | `test_dedup_window_ends_at_delivery_P3_R02` |
| P3-R03 | Each email gets its own envelope sender, `bounces+<outbox message id>@`, and a `Message-ID` from the same id. The record is saved as sending before the send, accepted after. | `test_return_address_per_email_P3_R03` |
| P3-R04 | A delivery report (RFC 3464) with `Action: failed` sent to that address marks the email bounced. The same report twice changes it once. An out-of-office reply or a delayed report changes nothing. Read mail is deleted after the database update. | `test_bounce_marks_the_email_once_P3_R04` |
| P3-R05 | Without a bounce, an email stays accepted. No status says delivered. | `test_no_bounce_is_not_delivered_P3_R05` |
| P3-R06 | If the SMTP answer is lost after the server took the email, the relay sends it again: two copies, with the same `Message-ID`. SMTP has no idempotency key. | `test_lost_answer_sends_twice_P3_R06` |
| P3-R07 | Dead-lettered messages and bounces are visible as metrics for an alert. A bounce read twice counts once. The post claims no alert for a stopped relay: grelmicro has no backlog gauge in this version. | `test_failures_are_metrics_P3_R07` |

## Not claimed

- Exactly once delivery. The database effect can be exactly once (one row, one queued message), the external send cannot. The posts keep the two apart.
- That SMTP's `Message-ID` header deduplicates. It does not: only a provider that documents an idempotency key and its window does.
- Inbox placement, or that a person saw the email. Without a bounce, the app knows only that a server took it.
- Success reports. The demo does not ask for them (DSN `NOTIFY=SUCCESS`), and many servers do not send them.
- Complaints (spam reports). They come through provider feedback loops, not SMTP bounces.
- That every mail server delivers `bounces+anything@` to `bounces@`. Most do (subaddressing), the post says to check.
- Ordering. grelmicro's outbox does not order messages in this version.

## grelmicro gaps found

Collected while building, reported to Loïc as candidate issues. Not fixed here.

- grelmicro's docs pages `/outbox/` and `/idempotency/` serve a redirect page that points to itself. Cause: `mkdocs.yml` maps `outbox.md: outbox/index.md` and `idempotency.md: idempotency/index.md`, both of which build to the same URL with directory URLs, so the redirect stub overwrites the real page. Subpages (`/outbox/producer/`) work.
- The outbox has no backlog gauge, so the post's alert rules cannot see a stopped relay. Comparing the rates of published, delivered and dead-lettered also misses a relay that stops while no new email comes in.
- The outbox has a Postgres and a memory backend, no SQLite. The rest of grelmicro (locks, tasks, cache) runs on SQLite, so a one-container app on day one cannot use the outbox without Postgres.
