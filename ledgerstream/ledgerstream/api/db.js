const { Pool } = require("pg");

// Support DATABASE_URL or PG_DSN connection string directly,
// otherwise fall back to local connection parameters.
const connectionString = process.env.DATABASE_URL || process.env.PG_DSN;
const pool = new Pool(
  connectionString
    ? { connectionString }
    : {
        database: "ledgerstream",
        user: "ledger",
        password: "ledger",
        host: process.env.PGHOST || "localhost",
        port: Number(process.env.PGPORT || 5433),
        max: 10,
      }
);

module.exports = { pool };