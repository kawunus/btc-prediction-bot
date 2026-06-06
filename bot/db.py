import aiomysql


class Database:
    def __init__(self, host, port, user, password, database):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self._pool = None

    async def connect(self):
        self._pool = await aiomysql.create_pool(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            db=self.database,
            autocommit=True,
            charset="utf8mb4",
            minsize=1,
            maxsize=5,
            echo=False,
            pool_recycle=3600,  # recycle connections every hour
        )

    async def close(self):
        if self._pool:
            self._pool.close()
            await self._pool.wait_closed()

    def _acquire(self):
        """Context manager that returns a connection with ping to handle stale connections."""
        return _PingConnection(self._pool)

    async def init_schema(self):
        async with self._acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS rounds (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        chat_id BIGINT DEFAULT NULL,
                        target_time VARCHAR(5) NOT NULL,
                        target_datetime DATETIME NOT NULL,
                        actual_price DECIMAL(20, 2) DEFAULT NULL,
                        winner_user_id BIGINT DEFAULT NULL,
                        winner_username VARCHAR(255) DEFAULT NULL,
                        winner_guess DECIMAL(20, 2) DEFAULT NULL,
                        is_active TINYINT(1) NOT NULL DEFAULT 1,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS guesses (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        round_id INT NOT NULL,
                        user_id BIGINT NOT NULL,
                        username VARCHAR(255),
                        first_name VARCHAR(255),
                        guess DECIMAL(20, 2) NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE KEY unique_guess (round_id, user_id),
                        FOREIGN KEY (round_id) REFERENCES rounds(id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS known_chats (
                        chat_id BIGINT PRIMARY KEY,
                        title VARCHAR(255),
                        added_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """)

    # --- Rounds ---

    async def create_round(self, target_time: str, target_datetime, chat_id: int = None) -> int:
        async with self._acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO rounds (chat_id, target_time, target_datetime) VALUES (%s, %s, %s)",
                    (chat_id, target_time, target_datetime),
                )
                return cur.lastrowid

    async def get_global_active_round(self):
        async with self._acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM rounds WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
                )
                return await cur.fetchone()

    async def get_round_by_id(self, round_id: int):
        async with self._acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("SELECT * FROM rounds WHERE id = %s", (round_id,))
                return await cur.fetchone()

    async def close_round(self, round_id: int, actual_price: float,
                          winner_user_id: int, winner_username: str, winner_guess: float):
        async with self._acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE rounds
                    SET is_active = 0,
                        actual_price = %s,
                        winner_user_id = %s,
                        winner_username = %s,
                        winner_guess = %s
                    WHERE id = %s
                    """,
                    (actual_price, winner_user_id, winner_username, winner_guess, round_id),
                )

    async def deactivate_round(self, round_id: int):
        async with self._acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE rounds SET is_active = 0 WHERE id = %s", (round_id,)
                )

    # --- Guesses ---

    async def add_guess(self, round_id: int, user_id: int, username: str,
                        first_name: str, guess: float) -> bool:
        async with self._acquire() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        "INSERT INTO guesses (round_id, user_id, username, first_name, guess) VALUES (%s, %s, %s, %s, %s)",
                        (round_id, user_id, username, first_name, guess),
                    )
                    return True
                except Exception:
                    return False

    async def get_guesses(self, round_id: int):
        async with self._acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM guesses WHERE round_id = %s", (round_id,)
                )
                return await cur.fetchall()

    async def get_all_active_rounds(self):
        async with self._acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM rounds WHERE is_active = 1"
                )
                return await cur.fetchall()

    # --- Known chats ---

    async def register_chat(self, chat_id: int, title: str):
        """Save group chat so we know where to broadcast results."""
        async with self._acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO known_chats (chat_id, title)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE title = VALUES(title)
                    """,
                    (chat_id, title or ""),
                )

    async def get_all_chats(self) -> list:
        async with self._acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("SELECT chat_id FROM known_chats")
                rows = await cur.fetchall()
                return [r["chat_id"] for r in rows]


class _PingConnection:
    """Wraps pool.acquire() and pings the connection before use."""

    def __init__(self, pool):
        self._pool = pool
        self._conn = None

    async def __aenter__(self):
        self._conn = await self._pool.acquire()
        try:
            await self._conn.ping(reconnect=True)
        except Exception:
            pass
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        self._pool.release(self._conn)
