# Environment files we supplied

Seven of the eighteen projects declare a service-level `env_file:` for a file they never
committed. That is not a failure to containerise: committing database passwords, mail
credentials and bot tokens would have been the worse practice, and those teams did the
right thing by leaving the file out. The gap has to be filled by whoever runs the code,
which here is us.

These files are that filling. They are written into the project immediately before
`docker compose up`, removed immediately after, and never committed into `projects/`
(which is gitignored anyway). The manifest names them per project under
`compose.materialize_env_files`, and every project that uses one carries a `provenance`
of `env_supplied` rather than `original` — the manifest's own validator refuses the
inconsistency, so a supplied file cannot silently be reported as a project that ran as
delivered.

## How the values were chosen

None of the seven ships an `.env.example`, so the variable names were read from the code
that consumes them (`process.env.X`) and from the `${...}` substitutions in each compose
file. Where compose already declares a default — `${POSTGRES_USER:-postgres}` — the
default is kept, so the file supplies only what is genuinely missing.

Three kinds of value appear here, and they are not equally consequential:

- **Database credentials and ports.** Free choices, constrained only by matching what the
  compose file passes to the database container. They affect nothing observable through
  the API.
- **Secrets** (`SESSION_SECRET`, `NEXTAUTH_SECRET`, encryption keys). Any value works; a
  fixed literal is used so runs are reproducible. These are throwaway values for a
  container that lives for a few minutes on a loopback port — they are not credentials.
- **External service settings** (SMTP, Telegram, Cloudinary). These are the consequential
  ones. No such service exists in the test environment, so they point at unreachable
  hosts. An endpoint whose success depends on sending mail will fail, and it will fail
  as an *environment* cause, not as a defect in the generated test. team13 is the
  documented case: its signup returns `400 "Failed to send verification email"`, which is
  why it gets a mail catcher rather than a plausible-looking SMTP host.

That last point is the reason this directory has a README. The values chosen here shape
what the generated tests can reach, exactly as the reconstructed OpenAPI specifications
shaped what the planner could see, and the results chapter has to be able to say so.
