const { Pool } = require("pg");

// Support DATABASE_URL or PG_DSN connection string directly,
// otherwise fall back to local connection parameters.
const connectionString = process.env.DATABASE_URL || process.env.PG_DSN;
let poolConfig;

function stripSslMode(dsn) {
  try {
    const parsed = new URL(dsn);
    parsed.searchParams.delete("sslmode");
    return parsed.toString();
  } catch {
    return dsn;
  }
}

if (connectionString) {
  const caCert = process.env.PG_SSL_CA || process.env.PG_CA_CERT || process.env.POSTGRES_CA_CERT;
  if (caCert) {
    // Strip sslmode query parameter from connectionString using URL API to prevent pg-connection-string
    // from overriding the explicit ssl object (ca certificate + rejectUnauthorized: true).
    poolConfig = {
      connectionString: stripSslMode(connectionString),
      ssl: {
        rejectUnauthorized: true,
        ca: caCert.replace(/\\n/g, "\n"),
      },
    };
  } else if (process.env.PG_SSL_REJECT_UNAUTHORIZED === "false") {
    poolConfig = {
      connectionString,
      ssl: { rejectUnauthorized: false },
    };
  } else {
    poolConfig = { connectionString };
  }
} else {
  poolConfig = {
    database: "ledgerstream",
    user: "ledger",
    password: "ledger",
    host: process.env.PGHOST || "localhost",
    port: Number(process.env.PGPORT || 5433),
    max: 10,
  };
}

const pool = new Pool(poolConfig);

module.exports = { pool };