from app import models, database

def init_db():
    models.Base.metadata.create_all(bind=database.engine)
    db = database.SessionLocal()

    try:
        # Check if test customer exists
        test_customer = db.query(models.Customer).filter(models.Customer.api_key == "test-api-key-123").first()
        if not test_customer:
            new_customer = models.Customer(
                name="Test Customer",
                api_key="test-api-key-123",
                daily_limit=100
            )
            db.add(new_customer)
            db.commit()
            print("Test customer created with API key: test-api-key-123")
        else:
            print("Test customer already exists.")
    finally:
        db.close()

if __name__ == "__main__":
    init_db()
