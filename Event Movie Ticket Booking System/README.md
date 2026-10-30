# Event & Movie Ticket Booking System

## Project Overview

The Event & Movie Ticket Booking System is a backend application developed using **FastAPI**. It allows users to browse events and movies, view available shows, select seats, book tickets, make payments, and receive digital tickets with QR codes.

The system also provides organizers and administrators with event, venue, show, booking, coupon, and sales management features.

## Technologies Used

- **Backend:** Python, FastAPI
- **Database:** MySQL
- **ORM:** SQLAlchemy
- **Database Migrations:** Alembic
- **Authentication:** JWT
- **Authorization:** Role-Based Access Control (RBAC)
- **Caching and Seat Locking:** Redis
- **Real-Time Updates:** WebSockets
- **Background Tasks:** FastAPI BackgroundTasks
- **Scheduled Jobs:** APScheduler
- **QR Code Generation:** qrcode
- **PDF Generation:** ReportLab
- **Excel Export:** OpenPyXL
- **Server:** Uvicorn
- **Containerization:** Docker and Docker Compose
- **Testing:** Pytest

## Main Features

### 1. User Authentication
- User registration and login.
- JWT-based authentication.
- Role-based access control.
- Protected API endpoints.

### 2. Venue Management
- Create and manage venues.
- Store venue address and city.
- Store latitude and longitude.
- Activate or deactivate venues.

### 3. Event Management
- Create and manage events.
- Organize events by category.
- Assign events to venues.
- Manage event details and schedules.

### 4. Movie Management
- Add and manage movies.
- Store movie title, category, language, and description.
- Maintain movie duration and age rating.

### 5. Show Management
- Create shows for events and movies.
- Associate shows with venues.
- Manage show schedules and ticket pricing.

### 6. Seat Management
- Display available seats.
- Select multiple seats.
- Lock seats temporarily using Redis.
- Prevent simultaneous bookings of the same seats.
- Provide real-time seat updates using WebSockets.

### 7. Ticket Booking
- Book tickets for selected shows.
- Validate seat availability.
- Apply coupon codes.
- Calculate booking amounts.
- Manage booking details.

### 8. Coupon Management
- Create and manage discount coupons.
- Support percentage-based and fixed discounts, according to configured coupon types.
- Configure minimum order amount and ticket count.
- Set coupon usage limits and expiry dates.

### 9. Payment Integration
- Integrate Razorpay for payment processing.
- Support payment verification.
- Maintain payment status and transaction details.

### 10. Digital Tickets
- Generate QR codes for booked tickets.
- Generate PDF tickets.
- Support QR-code-based ticket verification at entry.

### 11. Notifications
- Send booking-related email notifications.
- Use background tasks for email processing.
- Use scheduled jobs for automated tasks.

### 12. Reports and Excel Export
- Admin dashboard reports.
- Event sales reports.
- Show occupancy reports.
- Top event reports.
- Refund summary.
- Export event sales reports to Excel.

### 13. Pagination
- Support pagination for large API responses, including applicable venue, booking, and waitlist endpoints.


## Installation and Setup

### 1. Clone the Repository

```bash
git clone YOUR_GITHUB_REPOSITORY_URL
```

### 2. Navigate to the Project Folder

```bash
cd "Event Movie Ticket Booking System"
```

### 3. Create a Virtual Environment

```bash
python -m venv venv
```

### 4. Activate the Virtual Environment

Windows:

```bash
venv\Scripts\activate
```

### 5. Install Dependencies

```bash
pip install -r requirements.txt
```

### 6. Configure Environment Variables

Create a `.env` file in the project root directory.

Add the required configuration using your own credentials:

- env
DATABASE_URL=mysql+pymysql://username:password@localhost:3306/event_ticket_db

SECRET_KEY=your_jwt_secret_key
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=30

REDIS_URL=redis://localhost:6379/0

SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your_email@gmail.com
SMTP_PASSWORD=your_email_app_password

RAZORPAY_KEY_ID=your_razorpay_key_id
RAZORPAY_KEY_SECRET=your_razorpay_key_secret

**Security:** Never upload your actual `.env` file or secret credentials to GitHub. Add `.env` to `.gitignore`.

### 7. Create the MySQL Database

Create the database in MySQL:

```sql
CREATE DATABASE event_ticket_db;
```

Make sure your database credentials match the `.env` configuration.

### 8. Run Database Migrations

Apply Alembic migrations:

```bash
alembic upgrade head
```

### 9. Start Redis

Make sure the Redis server is running and the `REDIS_URL` is configured correctly.

### 10. Start the FastAPI Application

```bash
uvicorn app.main:app --reload
```

## Running with Docker

Make sure Docker Desktop is running.

Build and start the containers:

```bash
docker compose up --build -d
```

Check running containers:

```bash
docker compose ps
```

View application logs:

```bash
docker compose logs -f web
```

Stop the containers:

```bash
docker compose down
```

To remove containers and their stored volumes:

```bash
docker compose down -v
```

**Warning:** The `-v` option deletes the Docker database and Redis volumes. Use it only when you intend to remove that data.

## API Documentation

After starting the application, open the Swagger documentation:

**Swagger UI:**

http://127.0.0.1:8000/docs

**ReDoc:**

http://127.0.0.1:8000/redoc

Swagger UI allows users to:
- View available API endpoints.
- Test registration and login.
- Authorize using JWT access tokens.
- Create venues, events, movies, and shows.
- Manage seats, bookings, and coupons.
- Test payment and ticket-related APIs.
- Access reports and Excel export endpoints.

## Authentication

The application uses JWT authentication.

1. Register a user.
2. Log in using the registered credentials.
3. Copy the access token from the response.
4. Click the **Authorize** button in Swagger UI.
5. Enter the access token as required by the Bearer authentication configuration.
6. Access the protected endpoints according to the user's role.

## Testing

Run the test suite using:

```bash
pytest
```

For detailed test output:

```bash
pytest -v
```

## Important Security Practices

- Keep database credentials inside environment variables.
- Do not expose JWT or payment secrets.
- Do not commit `.env` to GitHub.
- Use role-based access control for protected operations.
- Validate booking and payment requests.
- Use Redis locking to reduce duplicate seat bookings.

## Future Enhancements

- Add additional payment gateway options.
- Improve analytics and reporting dashboards.
- Add advanced event recommendations.
- Enhance mobile application integration.

## Conclusion

The Event & Movie Ticket Booking System provides a structured backend solution for managing events, movies, venues, shows, seats, bookings, payments, and digital tickets. It uses FastAPI, MySQL, Redis, WebSockets, and other supporting technologies to provide secure booking operations and real-time functionality.