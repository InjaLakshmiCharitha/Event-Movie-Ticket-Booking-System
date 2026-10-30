from sqlalchemy import text
from app.database import engine
from app.redis_client import check_redis


def test_mysql():
    """Test the MySQL connection."""
    with engine.connect() as connection:
        database_name = connection.execute(
            text("SELECT DATABASE()")
        ).scalar()

    print("MySQL connection successful!")
    print(f"Connected database: {database_name}")


def test_redis():
    """Test the Redis connection."""
    if check_redis():
        print("Redis connection successful!")
    else:
        print("Redis connection failed!")


if __name__ == "__main__":
    try:
        test_mysql()
        test_redis()
    except Exception as error:
        print("Connection test failed.")
        print(f"Error: {error}")