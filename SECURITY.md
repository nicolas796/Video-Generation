# Security Guide for Product Video Generator

## Overview

This document outlines the security measures implemented in the Product Video Generator application.

## Authentication & Authorization

### Flask-Login
- All sensitive routes are protected with `@login_required` decorator
- Users must authenticate before accessing any application functionality
- Session management is handled securely by Flask-Login

### Default Admin User
- Set `ADMIN_USERNAME` and `ADMIN_PASSWORD` environment variables to create a default admin user
- The admin user is created automatically on first startup if these variables are set
- Passwords are hashed using Werkzeug's `generate_password_hash`

## CSRF Protection

### Flask-WTF CSRF
- All POST, PUT, DELETE forms include CSRF tokens
- CSRF protection is enforced globally via `CSRFProtect`
- Include `{{ csrf_token() }}` in all HTML forms

## Configuration Security

### SECRET_KEY
- **CRITICAL**: Must be set via `SECRET_KEY` environment variable in production
- In production, the app will fail to start if `SECRET_KEY` is not set
- For development, a random key is generated automatically (sessions won't persist across restarts)
- Never commit the SECRET_KEY to version control

### Debug Mode
- Debug mode is controlled by `FLASK_DEBUG` environment variable
- Defaults to `False` for security
- Never enable debug mode in production

## Path Traversal Protection

### File Upload Security
All file uploads are validated to prevent path traversal attacks:
- `secure_filename()` is used to sanitize filenames
- `is_safe_path()` helper function validates paths are within allowed directories
- `safe_join()` ensures joined paths don't escape the base directory

### Upload Routes Protected
- `/uploads/<path:filename>` - Validates filename before serving
- `/api/use-cases/<int:use_case_id>/upload-video` - Validates upload path

## Environment Variables

### Required for Production
```bash
SECRET_KEY=your-random-secret-key-min-32-chars
FLASK_ENV=production
FLASK_DEBUG=False
DATABASE_URL=postgresql://user:pass@localhost/dbname  # Recommended: use PostgreSQL
ADMIN_USERNAME=admin
ADMIN_PASSWORD=strong-password-here
```

### API Keys (Required for full functionality)
```bash
POLLO_API_KEY=your_pollo_key
ELEVENLABS_API_KEY=your_elevenlabs_key
OPENAI_API_KEY=your_openai_key
```

## Database Migrations

After deploying, run migrations to create the User table:

```bash
flask db migrate -m "Add User model for authentication"
flask db upgrade
```

## Security Checklist Before Production

- [ ] Set strong `SECRET_KEY` environment variable
- [ ] Set `FLASK_ENV=production`
- [ ] Set `FLASK_DEBUG=False`
- [ ] Set `ADMIN_USERNAME` and `ADMIN_PASSWORD` for initial admin
- [ ] Use HTTPS (configure reverse proxy)
- [ ] Use PostgreSQL instead of SQLite
- [ ] Configure proper logging
- [ ] Set up webhook signature verification (Pollo.ai)
- [ ] Review and restrict CORS if applicable
- [ ] Enable rate limiting (consider Flask-Limiter)
- [ ] Set up monitoring and alerting

## Reporting Security Issues

If you discover a security vulnerability, please report it immediately.
