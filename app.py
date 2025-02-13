from flask import Flask, request, session, flash, redirect, url_for, render_template, g, json, jsonify
import re
import traceback
import sys
from flask_bcrypt import Bcrypt
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from limits.storage import RedisStorage
from datetime import timedelta, datetime
from typing import List, Dict, Tuple, Optional
import sqlite3
import PyPDF2
import math
import string
import random
import os
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
import mysql.connector
from flask_mysqldb import MySQL
import io
from werkzeug.utils import secure_filename
import psycopg2
from psycopg2 import errors
from psycopg2.pool import SimpleConnectionPool
from functools import wraps
import logging
from logging.handlers import RotatingFileHandler

app = Flask(__name__)
logging.basicConfig(level=logging.DEBUG, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.FileHandler('app_debug.log'),
                        logging.StreamHandler()
                    ])

app.config.update(
    SECRET_KEY=os.urandom(24),  # Cryptographically secure random key
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=60),
    SESSION_COOKIE_SECURE=True,  # Ensure HTTPS
    SESSION_COOKIE_HTTPONLY=True,  # Prevent JavaScript access
    SESSION_COOKIE_SAMESITE='Lax',  # CSRF protection
)

app.config['DB_HOST'] = 'localhost'
app.config['DB_NAME'] = 'salespal'
app.config['DB_USER'] = 'yourusername'
app.config['DB_PASSWORD'] = 'yourpassword'  # Make sure this is correct

db_pool = SimpleConnectionPool(
    minconn=1,
    maxconn=10,
    host=app.config['DB_HOST'],
    database=app.config['DB_NAME'],
    user=app.config['DB_USER'],
    password=app.config['DB_PASSWORD']
)

def get_db():
    try:
        # Log connection attempt
        app.logger.debug("Attempting to get database connection from pool")
        connection = db_pool.getconn()
        
        # Optional: Verify connection is valid
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        
        return connection
    except Exception as e:
        app.logger.error(f"Failed to get database connection: {e}")
        return None

def return_db(conn):
    if conn:
        try:
            db_pool.putconn(conn)
            app.logger.debug("Connection returned to pool successfully")
        except Exception as e:
            app.logger.error(f"Error returning connection to pool: {e}")
        
def init_db():
    """
    Initialize database tables and create admin user if not exists.
    Handles database connection, table creation, and admin user setup.
    """
    db = None
    try:
        # Attempt to get a database connection
        db = get_db()
        if db is None:
            app.logger.error("Could not establish database connection")
            return False
        
        with db.cursor() as cursor:
            # Explicitly set search path to public schema
            cursor.execute('SET search_path TO public')
            
            # Create users table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) UNIQUE NOT NULL,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    phone VARCHAR(20) UNIQUE NOT NULL,
                    username VARCHAR(50) UNIQUE,
                    password TEXT,
                    approved INTEGER DEFAULT 0,
                    is_admin INTEGER DEFAULT 0,
                    rejected INTEGER DEFAULT 0
                );
            ''')
            
            # Create parsed_receipts table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS parsed_receipts (
                    id SERIAL PRIMARY KEY,
                    company_name TEXT,
                    customer TEXT,
                    order_date TEXT,
                    sales_person TEXT,
                    rq_invoice TEXT,
                    total_price REAL,
                    accessory_prices TEXT,
                    upgrades_count INTEGER,
                    activations_count INTEGER,
                    ppp_present BOOLEAN,
                    activation_fee_sum REAL,
                    user_id INTEGER,
                    date_submitted TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    imei_iccid_pairs TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
            ''')
            
            # Check if admin user exists
            cursor.execute("SELECT * FROM users WHERE username = 'admin'")
            admin_exists = cursor.fetchone()
            
            # Create default admin if it doesn't exist
            if not admin_exists:
                # Use a more secure default password generation
                import secrets
                default_password = secrets.token_urlsafe(12)  # Generate a more secure random password
                
                admin_password = bcrypt.generate_password_hash(default_password).decode('utf-8')
                cursor.execute('''
                    INSERT INTO users (name, email, phone, username, password, approved, is_admin)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                ''', ('Admin User', 'admin@example.com', '1234567890', 'admin', admin_password, 1, 1))
                
                # Log the generated password securely
                app.logger.info("Admin user created. Please change the default password.")
                print(f"IMPORTANT: Default admin password is: {default_password}")
            
            # Commit all changes
            db.commit()
            app.logger.info("Database tables and admin user created successfully")
            return True
    
    except Exception as e:
        # Comprehensive error logging
        app.logger.error(f"Database initialization error: {type(e).__name__}")
        app.logger.error(f"Error details: {str(e)}")
        app.logger.error(f"Error traceback: {traceback.format_exc()}")
        
        # Rollback in case of any error
        if db:
            db.rollback()
        
        return False
    
    finally:
        # Ensure database connection is properly closed
        if db:
            return_db(db)
            
@app.teardown_appcontext
def close_connection(exception):
    db = g.pop('db', None)
    if db is not None:
        try:
            return_db(db)
            app.logger.debug("Database connection closed")
        except Exception as e:
            app.logger.error(f"Error closing database connection: {e}")
            # Even if close fails, remove the reference
                
        
def create_admin_user():
    db = None
    try:
        db = get_db()
        if db is None:
            app.logger.error("Could not establish database connection")
            return
            
        with db.cursor() as cursor:
            # Check if admin user exists
            cursor.execute("SELECT * FROM users WHERE username = 'admin'")
            admin_exists = cursor.fetchone()
            
            # Create default admin if it doesn't exist
            if not admin_exists:
                admin_password = bcrypt.generate_password_hash('admin123').decode('utf-8')
                cursor.execute('''
                    INSERT INTO users (name, email, phone, username, password, approved, is_admin)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                ''', ('Admin User', 'admin@example.com', '1234567890', 'admin', admin_password, 1, 1))
                db.commit()
                app.logger.info("Default admin user created")
    except Exception as e:
        if db:
            db.rollback()
        app.logger.error(f"Error creating admin user: {str(e)}")
    finally:
        if db:
            return_db(db)

# Call this function when the app starts
with app.app_context():
    if init_db():
        app.logger.info("Database initialized successfully")
    else:
        app.logger.error("Failed to initialize database")
    create_admin_user()

app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')

if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user

handler = RotatingFileHandler('flask_app.log', maxBytes=10000, backupCount=3)
handler.setLevel(logging.ERROR)
app.logger.addHandler(handler)

# Initialize Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Create User class
class User(UserMixin):
    def __init__(self, id, name, is_admin):
        self.id = str(id)  # Flask-Login needs string ID
        self.name = name
        self.is_admin = is_admin

    def get_id(self):
        return self.id

logging.basicConfig(level=logging.DEBUG)
logging.getLogger().addHandler(logging.StreamHandler())

@app.errorhandler(500)
def handle_500(e):
    # Log the full traceback
    app.logger.error('An error occurred during a request.')
    app.logger.error(traceback.format_exc())
    
    # Optional: Log additional context
    app.logger.error(f"Exception: {str(e)}")
    app.logger.error(f"Request method: {request.method}")
    app.logger.error(f"Request URL: {request.url}")
    app.logger.error(f"Request data: {request.get_data()}")
    
    return "Internal Server Error", 500
    
# User loader callback
@login_manager.user_loader
def load_user(user_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id, name, is_admin FROM users WHERE id = %s", (user_id,))
    user = cursor.fetchone()
    if user:
        return User(
            id=user[0],
            name=user[1],
            is_admin=user[2]
        )
    return None
    
bcrypt = Bcrypt(app)
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    storage_uri="redis://localhost:6379",
    default_limits=["200 per day", "1000 per hour"]
)

@app.errorhandler(Exception)
def handle_exception(e):
    # Log the full stack trace
    app.logger.error('Unhandled exception', exc_info=True)
    
    # Log additional context
    app.logger.error(f"Exception Type: {type(e).__name__}")
    app.logger.error(f"Exception Details: {str(e)}")
    
    # Optional: Log request details
    try:
        app.logger.error(f"Request Method: {request.method}")
        app.logger.error(f"Request URL: {request.url}")
        app.logger.error(f"Request Headers: {request.headers}")
    except:
        pass
    
    return "Internal server error", 500

@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify(error="Rate limit exceeded. Please try again later."), 429

try:
    limiter.storage.storage.ping()
    app.logger.info("Successfully connected to Redis")
except Exception as e:
    app.logger.error(f"Failed to connect to Redis: {str(e)}")
    from limits.storage import MemoryStorage
    limiter.storage = MemoryStorage()

DATABASE = '/data/users.db'
os.makedirs('/home/ubuntu/SalesPal/data', exist_ok=True)  # Create the directory if it doesn't exist


def init_db():
    db = None
    try:
        db = get_db()
        if db is None:
            app.logger.error("Could not establish database connection")
            return
            
        with db.cursor() as cursor:
            # Explicitly set search path to public schema
            cursor.execute('SET search_path TO public')

            # Create users table first
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) UNIQUE NOT NULL,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    phone VARCHAR(20) UNIQUE NOT NULL,
                    username VARCHAR(50) UNIQUE,
                    password TEXT,
                    approved INTEGER DEFAULT 0,
                    is_admin INTEGER DEFAULT 0,
                    rejected INTEGER DEFAULT 0
                );
            ''')

            # Create parsed_receipts table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS parsed_receipts (
                    id SERIAL PRIMARY KEY,
                    company_name TEXT,
                    customer TEXT,
                    order_date TEXT,
                    sales_person TEXT,
                    rq_invoice TEXT,
                    total_price REAL,
                    accessory_prices TEXT,
                    upgrades_count INTEGER,
                    activations_count INTEGER,
                    ppp_present BOOLEAN,
                    activation_fee_sum REAL,
                    user_id INTEGER,
                    date_submitted TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    imei_iccid_pairs TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
            ''')

            # Check if admin user exists
            cursor.execute("SELECT * FROM users WHERE username = 'admin'")
            admin_exists = cursor.fetchone()
            
            # Create default admin if it doesn't exist
            if not admin_exists:
                admin_password = bcrypt.generate_password_hash('admin123').decode('utf-8')
                cursor.execute('''
                    INSERT INTO users (name, email, phone, username, password, approved, is_admin)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                ''', ('Admin User', 'admin@example.com', '1234567890', 'admin', admin_password, 1, 1))

            # Commit all changes
            db.commit()
            app.logger.info("Database tables and admin user created successfully")

    except Exception as e:
        app.logger.error(f"Detailed error in init_db: {type(e).__name__}")
        app.logger.error(f"Error message: {e}")
        if db:
            db.rollback()
        raise  # Re-raise to see full traceback
    finally:
        if db:
            db.close()

ALLOWED_EXTENSIONS = {'pdf'}

def allowed_file(filename):
    """Check if the uploaded file has an allowed extension"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/api/update_receipt/<string:rq_invoice>', methods=['POST'])
@login_required
def update_receipt_details(rq_invoice):
    if not request.is_json:
        return jsonify({'error': 'Content-Type must be application/json'}), 400

    try:
        db = get_db()
        cursor = db.cursor()
        
        # Verify user has permission to edit this receipt
        cursor.execute("""
            SELECT user_id, imei_iccid_pairs
            FROM parsed_receipts 
            WHERE rq_invoice = %s
        """, (rq_invoice,))
        
        receipt = cursor.fetchone()
        
        if not receipt:
            return jsonify({'error': 'Receipt not found'}), 404
        
        updates = request.get_json()

        # Prepare update query and values dynamically
        update_columns = []
        update_values = []

        # Mapping of frontend field names to database column names
        field_mapping = {
            'store': 'company_name',
            'customer': 'customer',
            'order_date': 'order_date',
            'sales_person': 'sales_person',
            'total_price': 'total_price',
            'accessories': 'accessory_prices',
            'activation_fee': 'activation_fee_sum',
            'upgrades': 'upgrades_count',
            'activations': 'activations_count',
            'ppp_present': 'ppp_present'
        }

        # Handle device info updates
        if 'device_info' in updates:
            try:
                # Validate device info format
                for device in updates['device_info']:
                    if not isinstance(device, dict):
                        return jsonify({'error': 'Invalid device info format'}), 400
                    if not all(key in device for key in ['imei', 'iccid']):
                        return jsonify({'error': 'Missing IMEI or ICCID'}), 400
                    if not re.match(r'^\d{15}$', str(device['imei'])):
                        return jsonify({'error': 'IMEI must be exactly 15 digits'}), 400
                    if not re.match(r'^\d{19,20}$', str(device['iccid'])):
                        return jsonify({'error': 'ICCID must be 19-20 digits'}), 400

                # Store device info as JSON
                device_info = json.dumps(updates['device_info'])
                update_columns.append('imei_iccid_pairs = %s')
                update_values.append(device_info)
                del updates['device_info']
            except (TypeError, ValueError) as e:
                return jsonify({'error': f'Invalid device info format: {str(e)}'}), 400

        # Process other updates
        for frontend_field, value in updates.items():
            # Map frontend field to database column
            if frontend_field in field_mapping:
                db_column = field_mapping[frontend_field]
                update_columns.append(f'{db_column} = %s')
                update_values.append(value)

        # Construct and execute update query
        if update_columns:
            update_query = f"""
                UPDATE parsed_receipts 
                SET {', '.join(update_columns)}
                WHERE rq_invoice = %s
            """
            update_values.append(rq_invoice)

            cursor.execute(update_query, tuple(update_values))
            db.commit()

        return jsonify({'message': 'Receipt updated successfully'})

    except Exception as e:
        db.rollback()
        print(f"Error updating receipt: {str(e)}")
        return jsonify({'error': str(e)}), 500
    
@app.route('/api/update_device_info/<int:receipt_id>', methods=['POST'])
def update_device_info(receipt_id):
    try:
        data = request.json
        
        cursor = mysql.connection.cursor()
        
        # Update IMEI/ICCID pairs
        for item in data:
            if 'imei' in item and 'iccid' in item:
                query = """
                    UPDATE device_info 
                    SET imei = %s, iccid = %s 
                    WHERE receipt_id = %s AND id = %s
                """
                cursor.execute(query, (item['imei'], item['iccid'], receipt_id, item['id']))
        
        mysql.connection.commit()
        cursor.close()
        
        return jsonify({'status': 'success', 'message': 'Device information updated successfully'})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500
    
@app.route('/non_admin_dashboard')
def non_admin_dashboard():
    if 'logged_in' not in session:
        return redirect(url_for('login'))
    
    # If the user is not an admin, show them the dashboard with the three boxes
    if 'admin' not in session:
        try:
            username = session.get('username')
            db = get_db()
            
            if db is None:
                app.logger.error("Could not establish database connection")
                return "Database connection error", 500
                
            with db.cursor() as cursor:
                # Query to get the user's name using the username
                cursor.execute("SELECT name FROM users WHERE username = %s", (username,))
                user = cursor.fetchone()
                
                if user:
                    # Pass the name to the template
                    return render_template('non_admin_dashboard.html', current_user=user[0])
                else:
                    # Fallback to username if name not found
                    return render_template('non_admin_dashboard.html', current_user=username)
                    
        except Exception as e:
            app.logger.error(f"Database error in non_admin_dashboard: {str(e)}")
            return "An error occurred", 500
        finally:
            if 'db' in locals():
                db.close()
    
    return redirect(url_for('admin_home'))

@app.route('/delete_receipt/<int:receipt_id>', methods=['POST'])
def delete_receipt(receipt_id):
    if 'logged_in' not in session:
        return redirect(url_for('login'))
    
    try:
        db = get_db()
        if db is None:
            app.logger.error("Could not establish database connection")
            return "Database connection error", 500
            
        with db.cursor() as cursor:
            # Delete the receipt from the database
            cursor.execute("DELETE FROM parsed_receipts WHERE id = %s", (receipt_id,))
            db.commit()
            
        return redirect(url_for('view_receipts'))
        
    except Exception as e:
        if db:
            db.rollback()
        app.logger.error(f"Error deleting receipt {receipt_id}: {str(e)}")
        return "Error deleting receipt", 500
    finally:
        if 'db' in locals():
            db.close()

def round_up(value, decimals=2):
    factor = 10 ** decimals
    return math.ceil(value * factor) / factor

# Close the database connection at the end of each request


@app.route('/admin/pending_accounts')
def pending_accounts():
    if 'admin' not in session:
        return redirect(url_for('login'))
        
    try:
        db = get_db()
        if db is None:
            app.logger.error("Could not establish database connection")
            return "Database connection error", 500
            
        with db.cursor() as cursor:
            # Select pending accounts for admin review
            cursor.execute("SELECT id, name, email, phone FROM users WHERE approved = 0")
            pending_users = cursor.fetchall()
            
        return render_template('pending_accounts.html', pending_users=pending_users)
        
    except Exception as e:
        app.logger.error(f"Error fetching pending accounts: {str(e)}")
        return "Error loading pending accounts", 500
    finally:
        if 'db' in locals():
            db.close()

# Route for registering new users
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        phone = request.form['phone']
        
        try:
            # Generate a random password
            password = generate_random_password(10)
            hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
            
            db = get_db()
            if db is None:
                app.logger.error("Could not establish database connection")
                return "Registration service temporarily unavailable", 500
                
            with db.cursor() as cursor:
                # Save user with empty username and approved set to 0
                cursor.execute(
                    "INSERT INTO users (name, email, phone, password, approved) VALUES (%s, %s, %s, %s, 0)",
                    (name, email, phone, hashed_password)
                )
                db.commit()
                
            # Send password to the user (e.g., via email)
            return "Your account has been created. Your password is: {}".format(password), 200
            
        except psycopg2.IntegrityError as e:
            db.rollback()
            app.logger.warning(f"Registration failed - duplicate entry: {str(e)}")
            return "Email or phone number already exists", 400
        except Exception as e:
            if 'db' in locals():
                db.rollback()
            app.logger.error(f"Registration error: {str(e)}")
            return "Error during registration", 500
        finally:
            if 'db' in locals():
                db.close()
    
    return render_template('register.html')

def generate_random_password(length, include_special_chars=False):
    characters = string.ascii_letters + string.digits
    if include_special_chars:
        characters += string.punctuation

    password = ''.join(random.choice(characters) for
 _ in range(length))
    
    return password

# Single login route with rate limiting
@app.route('/', methods=['GET', 'POST'])
@limiter.limit("20 per minute")
def login():
    print("=" * 50)
    print("LOGIN ROUTE ACCESSED")
    print("=" * 50)
    
    # Log request details
    print(f"Request Method: {request.method}")
    
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        
        # Detailed input logging
        print(f"Username Attempted: {username}")
        print(f"Password Length: {len(password)}")
        
        try:
            # Database connection
            db = get_db()
            if db is None:
                print("CRITICAL: Database connection failed")
                flash("Database connection error", "error")
                return render_template('login.html')
            
            with db.cursor() as cursor:
                # Execute query to find user
                cursor.execute("""
                    SELECT id, name, email, phone, username, password, approved, is_admin 
                    FROM users 
                    WHERE username = %s
                """, (username,))
                user_data = cursor.fetchone()
                
                # Detailed user search logging
                if user_data:
                    print("=" * 50)
                    print("USER FOUND IN DATABASE:")
                    print(f"User ID: {user_data[0]}")
                    print(f"Username: {user_data[4]}")
                    print(f"Approved Status: {user_data[6]}")
                    print(f"Is Admin: {user_data[7]}")
                    print("=" * 50)
                else:
                    print("NO USER FOUND with username: " + username)
                
                # Password verification
                if user_data:
                    try:
                        # Verify password
                        is_password_correct = bcrypt.check_password_hash(user_data[5], password)
                        print(f"Password Verification Result: {is_password_correct}")
                    except Exception as hash_error:
                        print("PASSWORD HASH ERROR:")
                        print(traceback.format_exc())
                        is_password_correct = False
                else:
                    is_password_correct = False
                
                # Authentication logic
                if user_data and is_password_correct:
                    # Check if user is approved
                    if user_data[6] == 1:  # Approved
                        # Clear existing session
                        session.clear()
                        
                        # Set session variables
                        session['logged_in'] = True
                        session['username'] = username
                        session['user_id'] = user_data[0]
                        
                        # Set admin flag if applicable
                        if user_data[7] == 1:
                            session['admin'] = True
                            print("Redirecting to ADMIN home")
                            return redirect(url_for('admin_home'))
                        else:
                            print("Redirecting to NON-ADMIN dashboard")
                            return redirect(url_for('non_admin_dashboard'))
                    else:
                        print("User not approved")
                        flash("Your account is not approved", "error")
                else:
                    print("Authentication FAILED")
                    flash("Invalid username or password", "error")
                
                # Always return to login page if authentication fails
                return render_template('login.html')
        
        except Exception as e:
            print("UNEXPECTED LOGIN ERROR:")
            print(traceback.format_exc())
            flash("An unexpected error occurred", "error")
            return render_template('login.html')
    
    # GET request handling
    return render_template('login.html')
# Home route

def reset_user_password(username, new_password):
    db = get_db()
    try:
        with db.cursor() as cursor:
            # Hash the new password
            hashed_password = bcrypt.generate_password_hash(new_password).decode('utf-8')
            
            # Update user password and ensure approved
            cursor.execute("""
                UPDATE users 
                SET password = %s, approved = 1, is_admin = 1
                WHERE username = %s
            """, (hashed_password, username))
            
            db.commit()
            print(f"Password reset for user: {username}")
            print(f"Hashed Password: {hashed_password}")
    except Exception as e:
        print(f"Password reset error: {e}")
        db.rollback()
    finally:
        db.close()

# Example usage (run in Python console)
reset_user_password('hello', 'newpassword123')

@app.route('/home')
@login_required  # Ensure user is logged in
def home():
    try:
        # Debugging print statements
        print("Home route accessed")
        print(f"Session contents: {dict(session)}")
        
        # Check if the user is an admin
        if session.get('admin', False):
            print("Redirecting admin to employee list")
            return redirect(url_for('employee_list'))
        else:
            print("Redirecting non-admin to upload PDF")
            return redirect(url_for('upload_pdf'))
    
    except Exception as e:
        # Log any unexpected errors
        app.logger.error(f"Error in home route: {e}")
        flash("An error occurred", "error")
        return redirect(url_for('login'))

@app.route('/admin/home')
def admin_home():
    if 'admin' not in session:
        return redirect(url_for('login'))
        
    try:
        db = get_db()
        if db is None:
            app.logger.error("Could not establish database connection")
            return "Database connection error", 500
            
        with db.cursor() as cursor:
            cursor.execute("SELECT name FROM users WHERE id = %s", (session['user_id'],))
            user = cursor.fetchone()
            current_user = user[0] if user else 'User'
            
        return render_template('admin_home.html', current_user=current_user)
        
    except Exception as e:
        app.logger.error(f"Error in admin home: {str(e)}")
        return "Error loading admin home", 500
    finally:
        if 'db' in locals():
            db.close()

# Admin page to list employees and approve/reject accounts
@app.route('/admin/employees')
def employee_list():
    if 'admin' not in session:
        return redirect(url_for('login'))
        
    try:
        db = get_db()
        if db is None:
            app.logger.error("Could not establish database connection")
            return "Database connection error", 500
            
        with db.cursor() as cursor:
            # Get all employees data
            cursor.execute("SELECT id, name, email, phone, username, approved, is_admin, rejected FROM users")
            employees = cursor.fetchall()
            
            # Get current user's name
            cursor.execute("SELECT name FROM users WHERE id = %s", (session['user_id'],))
            user = cursor.fetchone()
            current_user = user[0] if user else 'User'
            
        return render_template('employee_list.html', 
                             employees=employees, 
                             current_user=current_user)
                             
    except Exception as e:
        app.logger.error(f"Error in employee list: {str(e)}")
        return "Error loading employee list", 500
    finally:
        if 'db' in locals():
            db.close()

@app.route('/admin/commission')
def view_commission():
    if 'admin' not in session:
        return redirect(url_for('login'))
    
    return "<h1>Commission Information Page</h1><p>This page will show commissions of all employees.</p>"

@app.route('/admin/assign_username/<int:user_id>', methods=['POST'])
def assign_username(user_id):
    if 'admin' not in session:
        return redirect(url_for('login'))

    username = request.form['username']

    db = get_db()
    cursor = db.cursor()

    cursor.execute("UPDATE users SET username = %s WHERE id = %s", (username, user_id))
    db.commit()

    return redirect(url_for('employee_list'))

@app.route('/admin/approve_user/<int:user_id>', methods=['POST'])
def approve_user_account(user_id):
    if 'admin' not in session:
        return redirect(url_for('login'))

    db = get_db()
    cursor = db.cursor()
    
    # Set approved status to 1
    cursor.execute("UPDATE users SET approved = 1 WHERE id = %s", (user_id,))
    db.commit()
    
    return redirect(url_for('employee_list'))

@app.route('/admin/approve/<int:user_id>', methods=['POST'])
def approve_account(user_id):
    if 'admin' not in session:
        return redirect(url_for('login'))

    db = get_db()
    cursor = db.cursor()

    # Update the user's approved status to 1
    cursor.execute("UPDATE users SET approved = 1, rejected = 0 WHERE id = %s", (user_id,))
    db.commit()

    return redirect(url_for('employee_list'))

@app.route('/admin/reject/<int:user_id>', methods=['POST'])
def reject_account(user_id):
    if 'admin' not in session:
        return redirect(url_for('login'))

    db = get_db()
    cursor = db.cursor()

    # Set the rejected flag to 1
    cursor.execute("UPDATE users SET rejected = 1, approved = 0 WHERE id = %s", (user_id,))
    db.commit()

    return redirect(url_for('employee_list'))

@app.route('/admin/delete/<int:user_id>', methods=['POST'])
def delete_account(user_id):
    if 'admin' not in session:
        return redirect(url_for('login'))

    db = get_db()
    cursor = db.cursor()
    
    # Delete the user by ID
    cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
    db.commit()
    
    return redirect(url_for('employee_list'))

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    session.pop('username', None)
    session.pop('admin', None)
    return redirect(url_for('login'))

def extract_imei_iccid_pairs(text):
    """
    Extract IMEI and ICCID pairs in order of appearance in the document.
    """
    pairs = []
    lines = text.split('\n')
    current_imei = None
    
    for line in lines:
        if 'IMEI:' in line:
            imei_match = re.search(r'IMEI:(\d{15})', line)
            if imei_match:
                current_imei = imei_match.group(1)
        elif 'ICCID:' in line and current_imei:
            iccid_match = re.search(r'ICCID:(\d{20})', line)
            if iccid_match:
                iccid = iccid_match.group(1)
                pairs.append({
                    'imei': current_imei,
                    'iccid': iccid
                })
                current_imei = None  # Reset current_imei after creating a pair
    
    return pairs

def pair_imei_iccid(imeis: List[str], iccids: List[str]) -> List[Dict[str, str]]:
    """Create pairs of IMEI and ICCID numbers preserving order."""
    pairs = []
    
    # Create pairs while maintaining order
    for i in range(min(len(imeis), len(iccids))):
        pairs.append({
            'imei': imeis[i],
            'iccid': iccids[i]
        })
    
    return pairs

def extract_info_from_pdf(pdf_file) -> Tuple:
    """Extract all information from PDF file."""
    reader = PyPDF2.PdfReader(pdf_file)
    pdf_text = ""
    
    for page in reader.pages:
        pdf_text += page.extract_text()

    # Regular expression patterns
    company_pattern = r"Sale\nR\d+\n(\d{3}:\s[A-Za-z\s]+)"
    customer_pattern = r"Customer\s*(.*%s)(%s:\n|\s*\()"
    order_date_pattern = r"Order Date\s*(\d{1,2}-\w{3}-\d{4}\s*\d{1,2}:\d{2}:\d{2}\s*\w*)"
    sales_person_pattern = r"Tendered By:\s*(.*%s)(%s:\n|$)"
    rq_invoice_pattern = r"Sale\n(R\d+)\n"
    
    # Get IMEI/ICCID pairs
    imei_iccid_pairs = extract_imei_iccid_pairs(pdf_text)
    
    # Other patterns
    upgrades_pattern = r"\bUpgrade Fee\b"
    activations_pattern = r"\bActivation Fee\b"
    ppp_pattern = r"\bLease\b"
    activation_fee_pattern = r"Fee\s*\d\s*@\$\s*([\d.]+)"

    # Extract data
    company_name = re.search(company_pattern, pdf_text, re.DOTALL)
    customer = re.search(customer_pattern, pdf_text, re.DOTALL)
    order_date = re.search(order_date_pattern, pdf_text, re.DOTALL)
    sales_person = re.search(sales_person_pattern, pdf_text, re.DOTALL)
    rq_invoice = re.search(rq_invoice_pattern, pdf_text, re.DOTALL)
    
    # Count occurrences
    upgrades_count = len(re.findall(upgrades_pattern, pdf_text, re.IGNORECASE))
    activations_count = len(re.findall(activations_pattern, pdf_text, re.IGNORECASE))
    ppp_present = bool(re.search(ppp_pattern, pdf_text, re.IGNORECASE))
    
    # Calculate activation fees
    activation_fees = re.findall(activation_fee_pattern, pdf_text)
    activation_fee_sum = round(sum(float(fee) for fee in activation_fees), 2)

    # Calculate accessories (moved to separate function)
    total_price, accessory_prices = calculate_accessories(pdf_text)

    return (
        company_name.group(1).strip() if company_name else "N/A",
        customer.group(1).strip() if customer else "N/A",
        order_date.group(1).strip() if order_date else "N/A",
        sales_person.group(1).strip() if sales_person else "N/A",
        rq_invoice.group(1).strip() if rq_invoice else "N/A",
        total_price,
        accessory_prices,
        upgrades_count,
        activations_count,
        ppp_present,
        imei_iccid_pairs,
        activation_fee_sum
    )

def calculate_accessories(pdf_text: str) -> Tuple[float, List[float]]:
    """Calculate accessory prices from PDF text."""
    accessory_pattern = r'([A-Z0-9]+)\n(.*%s)\n(%s:.*%s@\$(\d+\.\d+)).*%sItem Total\s+\$(\d+\.\d+)'
    non_accessory_identifiers = [
        'DEFBYOD', 'UNLCOR', 'UNLMORE', 'ACTIVATION',
        'IMEI:', 'ICCID:', 'SIM', 'STHN', 'SSGN',
        '55UNL', '60UNL'
    ]
    
    accessory_prices = []
    for match in re.finditer(accessory_pattern, pdf_text, re.DOTALL):
        sku = match.group(1)
        description = match.group(2).strip()
        final_price = float(match.group(4))
        
        if not any(identifier in sku or identifier in description 
                  for identifier in non_accessory_identifiers):
            accessory_prices.append(round(final_price, 2))
    
    total_price = round(sum(accessory_prices), 2)
    return total_price, accessory_prices

# PDF upload page
@app.route('/upload', methods=['GET', 'POST'])
def upload_pdf():
    if 'logged_in' not in session:
        return redirect(url_for('login'))

    # Handle GET request
    if request.method == 'GET':
        try:
            db = get_db()
            if db is None:
                app.logger.error("Could not establish database connection")
                return "Database connection error", 500
                
            with db.cursor() as cursor:
                cursor.execute("SELECT name FROM users WHERE id = %s", (session['user_id'],))
                user = cursor.fetchone()
                current_user = user[0] if user else 'User'
                
            return render_template('upload.html', current_user=current_user)
            
        except Exception as e:
            app.logger.error(f"Database error in upload GET: {str(e)}")
            return "Error loading upload page", 500
        finally:
            if 'db' in locals():
                db.close()

    # Handle POST request - file upload
    try:
        # Validate request content type
        if not request.files:
            app.logger.warning("No files in request")
            return jsonify({'error': 'No files were uploaded'}), 400

        # Check file presence and type
        if 'pdf[]' not in request.files and 'pdf' not in request.files:
            app.logger.warning("Missing PDF files in request")
            return jsonify({'error': 'No PDF files were uploaded'}), 400

        # Handle both multiple and single file uploads
        files = request.files.getlist('pdf[]') if 'pdf[]' in request.files else [request.files['pdf']]

        # Validate files existence
        if not files or not any(file.filename for file in files):
            app.logger.warning("No files selected")
            return jsonify({'error': 'No files selected'}), 400

        # Validate file size
        for file in files:
            if file and file.filename:
                file.seek(0, os.SEEK_END)
                size = file.tell()
                file.seek(0)
                if size > app.config['MAX_CONTENT_LENGTH']:
                    app.logger.warning(f"File {file.filename} exceeds size limit")
                    return jsonify({'error': f'File {file.filename} exceeds maximum size limit of {app.config["MAX_CONTENT_LENGTH"] // (1024*1024)}MB'}), 413

        uploaded_files = []
        errors = []
        parsed_data_list = []
        
        app.logger.info(f"Processing {len(files)} files")
        
        for file in files:
            if file and file.filename and allowed_file(file.filename):
                try:
                    filename = secure_filename(file.filename)
                    app.logger.info(f"Processing file: {filename}")
                    
                    # Validate file content
                    try:
                        file_content = file.read()
                        if not file_content:
                            raise ValueError("Empty file")
                            
                        file_stream = io.BytesIO(file_content)
                        
                        # Validate PDF format
                        try:
                            reader = PyPDF2.PdfReader(file_stream)
                            if len(reader.pages) == 0:
                                raise ValueError("PDF has no pages")
                                
                            pdf_text = "".join(page.extract_text() for page in reader.pages)
                            if not pdf_text.strip():
                                raise ValueError("PDF contains no text")
                                
                            # Reset file stream for extract_info_from_pdf
                            file_stream.seek(0)
                            
                            # Extract information with validation
                            result = extract_info_from_pdf(file_stream)
                            if not all(result):
                                raise ValueError("Failed to extract required information from PDF")
                                
                            (company_name, customer, order_date, sales_person, rq_invoice, 
                             total_price, accessories_prices, upgrades_count, activations_count, 
                             ppp_present, pairs, activation_fee_sum) = result
                            
                            parsed_data = {
                                'filename': filename,
                                'company_name': company_name,
                                'customer': customer,
                                'order_date': order_date,
                                'sales_person': sales_person,
                                'rq_invoice': rq_invoice,
                                'total_price': total_price,
                                'accessories_prices': accessories_prices,
                                'upgrades_count': upgrades_count,
                                'activations_count': activations_count,
                                'ppp_present': ppp_present,
                                'activation_fee_sum': activation_fee_sum,
                                'imei_iccid_pairs': pairs,
                                'pdf_text': pdf_text
                            }
                            
                            parsed_data_list.append(parsed_data)
                            uploaded_files.append(filename)
                            app.logger.info(f"Successfully processed {filename}")
                            
                        except PyPDF2.PdfReadError as e:
                            raise ValueError(f"Invalid PDF format: {str(e)}")
                            
                    except ValueError as e:
                        raise ValueError(f"Content validation failed: {str(e)}")
                        
                except Exception as e:
                    app.logger.error(f"Error processing {file.filename}: {str(e)}")
                    errors.append(f"Error processing {file.filename}: {str(e)}")
            else:
                error_msg = f"Invalid file: {file.filename if file.filename else 'No file selected'}"
                app.logger.warning(error_msg)
                errors.append(error_msg)

        if not parsed_data_list:
            if errors:
                return jsonify({'error': ' | '.join(errors)}), 400
            return jsonify({'error': 'No valid files were processed'}), 400

        # Store the list of parsed data in session with size validation
        try:
            session['parsed_data_list'] = parsed_data_list
            session['current_pdf_index'] = 0
            session.modified = True
        except Exception as e:
            app.logger.error(f"Session storage error: {str(e)}")
            return jsonify({'error': 'Error storing processed data'}), 500

        response_data = {
            'status': 'success',
            'message': f'Successfully processed {len(uploaded_files)} files',
            'uploaded': uploaded_files,
            'redirect': url_for('confirm_receipt')
        }
        app.logger.info(f"Upload complete: {response_data}")
        return jsonify(response_data)
        
    except Exception as e:
        app.logger.error(f"Unexpected error in upload: {str(e)}")
        return jsonify({'error': 'An unexpected error occurred during upload'}), 500
    
@app.route('/confirm', methods=['GET', 'POST'])
def confirm_receipt():
    if 'logged_in' not in session:
        return redirect(url_for('login'))
    
    if 'parsed_data_list' not in session:
        return redirect(url_for('upload_pdf'))
    
    parsed_data_list = session.get('parsed_data_list', [])
    current_index = session.get('current_pdf_index', 0)
    
    if current_index >= len(parsed_data_list):
        # All PDFs have been processed
        session.pop('parsed_data_list', None)
        session.pop('current_pdf_index', None)
        return redirect(url_for('view_receipts'))
    
    # Get current PDF data
    current_pdf = parsed_data_list[current_index]
    
    # Get logged in user's name
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT name FROM users WHERE id = %s", (session['user_id'],))
    user = cursor.fetchone()
    logged_in_user = user[0] if user else None
    
    if request.method == 'POST':
        if not session.get('user_id'):
            return redirect(url_for('login'))
        
        # Process form data
        form_data = {
            'company_name': request.form.get('company_name', 'N/A'),
            'customer': request.form.get('customer', 'N/A'),
            'order_date': request.form.get('order_date', 'N/A'),
            'sales_person': request.form.get('sales_person', 'N/A'),
            'rq_invoice': request.form.get('rq_invoice', 'N/A'),
            'total_price': float(request.form.get('total_price', 0)),
            'accessories_prices': request.form.get('accessories_prices', ''),
            'upgrades_count': int(request.form.get('upgrades_count', 0)),
            'activations_count': int(request.form.get('activations_count', 0)),
            'ppp_present': 'ppp_present' in request.form,
            'activation_fee_sum': float(request.form.get('activation_fee_sum', 0))
        }

        imei_iccid_pairs = current_pdf.get('imei_iccid_pairs', [])
        
        # Convert pairs to JSON string for storage
        imei_iccid_json = json.dumps(imei_iccid_pairs)
        
        
        # Save to database
        cursor.execute('''
            INSERT INTO parsed_receipts (
                company_name, customer, order_date, sales_person, rq_invoice, 
                total_price, accessory_prices, upgrades_count, activations_count, 
                ppp_present, activation_fee_sum, user_id, imei_iccid_pairs
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (
            form_data['company_name'], form_data['customer'], form_data['order_date'],
            form_data['sales_person'], form_data['rq_invoice'], form_data['total_price'],
            form_data['accessories_prices'], form_data['upgrades_count'],
            form_data['activations_count'], form_data['ppp_present'],
            form_data['activation_fee_sum'], session['user_id'], imei_iccid_json
        ))
        
        db.commit()
        
        # Move to next PDF
        session['current_pdf_index'] = current_index + 1
        
        if current_index + 1 >= len(parsed_data_list):
            # All PDFs processed
            session.pop('parsed_data_list', None)
            session.pop('current_pdf_index', None)
            return redirect(url_for('view_receipts'))
        
        return redirect(url_for('confirm_receipt'))
    
    # For GET request, display the current PDF's data
    current_pdf['logged_in_user'] = logged_in_user
    total_pdfs = len(parsed_data_list)
    current_number = current_index + 1
    
    return render_template('confirm_receipt.html', 
                         current_user=logged_in_user,
                         total_pdfs=total_pdfs,
                         current_pdf_number=current_number,
                         **current_pdf)

@app.route('/view_receipts')
def view_receipts():
    if 'logged_in' not in session:
        return redirect(url_for('login'))
    
    db = get_db()
    cursor = db.cursor()

    # Get the current user's name
    cursor.execute("SELECT name FROM users WHERE id = %s", (session['user_id'],))
    user_data = cursor.fetchone()
    current_user = user_data[0] if user_data else 'User'

    # Fetch user details to check if the logged-in user is an admin
    cursor.execute("SELECT * FROM users WHERE id = %s", (session['user_id'],))
    user = cursor.fetchone()
    
    if user and user[7] == 1:  # Admin user
        cursor.execute("""
            SELECT 
             r.*, u.name as uploader_name
            FROM parsed_receipts r
            LEFT JOIN users u ON r.user_id = u.id
            ORDER BY r.date_submitted DESC
        """)
    else:
        cursor.execute("""
            SELECT 
             r.*, u.name as uploader_name
            FROM parsed_receipts r
            LEFT JOIN users u ON r.user_id = u.id
            WHERE r.user_id = %s
        """, (session['user_id'],))
    
    receipts = cursor.fetchall()
    return render_template('view_receipts.html', receipts=receipts, current_user=current_user)

@app.route('/receipt_details/<string:rq_invoice>')
def receipt_details(rq_invoice):
    if 'logged_in' not in session:
        return redirect(url_for('login'))

    db = get_db()
    cursor = db.cursor()

    # Fetch user details to check if admin
    cursor.execute("SELECT id, name, is_admin FROM users WHERE id = %s", (session['user_id'],))
    user = cursor.fetchone()
    is_admin = user and user[2] == 1
    current_user = user[1] if user else 'User'

    # Fetch receipt details
    if is_admin:
        cursor.execute("""
            SELECT 
                r.id,
                r.company_name,
                r.customer,
                r.order_date,
                r.sales_person,
                r.rq_invoice,
                r.total_price,
                r.accessory_prices,
                r.upgrades_count,
                r.activations_count,
                r.ppp_present,
                r.activation_fee_sum,
                r.imei_iccid_pairs,
                u.name as uploader_name
            FROM parsed_receipts r
            LEFT JOIN users u ON r.user_id = u.id
            WHERE r.rq_invoice = %s
        """, (rq_invoice,))
    else:
        cursor.execute("""
            SELECT 
                r.id,
                r.company_name,
                r.customer,
                r.order_date,
                r.sales_person,
                r.rq_invoice,
                r.total_price,
                r.accessory_prices,
                r.upgrades_count,
                r.activations_count,
                r.ppp_present,
                r.activation_fee_sum,
                r.imei_iccid_pairs,
                u.name as uploader_name
            FROM parsed_receipts r
            LEFT JOIN users u ON r.user_id = u.id
            WHERE r.rq_invoice = %s AND r.user_id = %s
        """, (rq_invoice, session['user_id']))

    receipt = cursor.fetchone()
    if not receipt:
        return "Receipt not found", 404
    
    imei_iccid_pairs = []
    if receipt and receipt[12]:  # Assuming imei_iccid_pairs is the last column
        try:
            imei_iccid_pairs = json.loads(receipt[12])
        except (json.JSONDecodeError, TypeError):
            pass

    return render_template('receipt_details.html', receipt=receipt, imei_iccid_pairs=imei_iccid_pairs, current_user=current_user)

@app.route('/commission')
def commission():
    if 'logged_in' not in session:
        return redirect(url_for('login'))

    db = get_db()
    cursor = db.cursor()
    
    # Check if user is admin
    cursor.execute("SELECT is_admin FROM users WHERE id = %s", (session['user_id'],))
    user = cursor.fetchone()
    is_admin = user and user[0] == 1
    current_user = user[0] if user else 'User'

    if is_admin:
        cursor.execute('''
            SELECT 
                users.username, 
                users.name,
                SUM(COALESCE(parsed_receipts.activations_count, 0)) as total_activations,
                SUM(COALESCE(parsed_receipts.upgrades_count, 0)) as total_upgrades,
                SUM(COALESCE(parsed_receipts.activations_count, 0) + COALESCE(parsed_receipts.upgrades_count, 0)) as total_devices,
                COALESCE(SUM(parsed_receipts.total_price), 0) as total_accessories,
                CASE 
                    WHEN COALESCE(SUM(parsed_receipts.total_price), 0) >= 1750 THEN 4
                    WHEN COALESCE(SUM(parsed_receipts.total_price), 0) >= 1000 THEN 3
                    WHEN COALESCE(SUM(parsed_receipts.total_price), 0) >= 750 THEN 2
                    WHEN COALESCE(SUM(parsed_receipts.total_price), 0) >= 500 THEN 1
                    ELSE 1
                END as current_tier
            FROM 
                users
            LEFT JOIN 
                parsed_receipts ON users.id = parsed_receipts.user_id
            WHERE
                users.is_admin = 0
            GROUP BY 
                users.username, users.name
        ''')
        commission_data = cursor.fetchall()
        return render_template('commission.html', 
                             commission_data=commission_data, 
                             is_admin=is_admin,
                             current_user=current_user)
    else:
        cursor.execute('''
            SELECT 
                users.username, 
                users.name,
                SUM(COALESCE(parsed_receipts.activations_count, 0)) as total_activations,
                SUM(COALESCE(parsed_receipts.upgrades_count, 0)) as total_upgrades,
                SUM(COALESCE(parsed_receipts.activations_count, 0) + COALESCE(parsed_receipts.upgrades_count, 0)) as total_devices,
                COALESCE(SUM(parsed_receipts.total_price), 0) as total_accessories,
                CASE 
                    WHEN COALESCE(SUM(parsed_receipts.total_price), 0) >= 1750 THEN 4
                    WHEN COALESCE(SUM(parsed_receipts.total_price), 0) >= 1000 THEN 3
                    WHEN COALESCE(SUM(parsed_receipts.total_price), 0) >= 750 THEN 2
                    WHEN COALESCE(SUM(parsed_receipts.total_price), 0) >= 500 THEN 1
                    ELSE 1
                END as current_tier
            FROM 
                users
            LEFT JOIN 
                parsed_receipts ON users.id = parsed_receipts.user_id
            WHERE 
                users.id = %s
            GROUP BY 
                users.username, users.name
        ''', (session['user_id'],))

        commission_data = cursor.fetchall()
        
        # Calculate accessories total and progress
        accessories_total = commission_data[0][5] if commission_data else 0
        progress = min((float(accessories_total) / 1750 * 100), 100)
        
        # Get tier from the query result
        current_tier = commission_data[0][6] if commission_data else 1

        return render_template('commission.html', 
                             commission_data=commission_data, 
                             is_admin=is_admin,
                             accessories_total=accessories_total,
                             current_tier=current_tier,
                             progress=progress,
                             current_user=current_user)

if __name__ == '__main__':
    with app.app_context():
        init_db()  # Initialize the database tables
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port,debug=True)
