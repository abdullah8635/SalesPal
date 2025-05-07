from flask import Flask, request, abort, session, flash, redirect, url_for, render_template, g, json, jsonify
import re
import traceback
import sys
from flask_bcrypt import Bcrypt
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from limits.storage import RedisStorage
from datetime import timedelta, datetime
from typing import List, Dict, Tuple, Optional
import PyPDF2
from PyPDF2.errors import PdfReadError
import math
import string
import random
import os
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
import mysql.connector
from flask_mysqldb import MySQL
import io
from werkzeug.utils import secure_filename
from werkzeug.exceptions import NotFound
import psycopg2
from psycopg2 import errors
from contextlib import contextmanager
from psycopg2.extras import DictCursor
from psycopg2.pool import SimpleConnectionPool
from functools import wraps
import logging
from logging.handlers import RotatingFileHandler

app = Flask(__name__)
login_manager = LoginManager(app)
login_manager.init_app(app)
login_manager.login_view = 'login'

logging.basicConfig(level=logging.DEBUG, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.FileHandler('app_debug.log'),
                        logging.StreamHandler()
                    ])

app.config.update(
    SECRET_KEY='hello',  # Cryptographically secure random key
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=60),
    SESSION_COOKIE_SECURE=True,  # Ensure HTTPS
    SESSION_COOKIE_HTTPONLY=True,  # Prevent JavaScript access
    SESSION_COOKIE_SAMESITE='Lax', 
    DB_HOST = 'localhost',
    DB_NAME = 'salespal',
    DB_USER = 'yourusername',
    DB_PASSWORD = 'yourpassword'  # CSRF protection
)

db_pool = SimpleConnectionPool(
    minconn=1,
    maxconn=10000,
    host=app.config['DB_HOST'],
    database=app.config['DB_NAME'],
    user=app.config['DB_USER'],
    password=app.config['DB_PASSWORD'],
    keepalives=1,
    keepalives_idle=30,
    keepalives_interval=10,
    keepalives_count=5
)

bcrypt = Bcrypt(app)
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    storage_uri="redis://localhost:6379",
    default_limits=["200 per day", "1000 per hour"]
)

@contextmanager
def get_db_connection():
    conn = None
    try:
        conn = get_db()
        yield conn
    finally:
        if conn:
            return_db(conn)
@app.errorhandler(404)
def handle_404(e):
    # Log the suspicious reques
    
    # Optional: Implement more sophisticated blocking
    # For example, block IPs with too many 404 requests
    return "Not Found", 404

@app.before_request
def block_suspicious_paths():
    suspicious_paths = [
        '/wp-includes',
        '/xmlrpc.php',
        '/wp-login.php',
        '/feed/'
    ]
    
    for path in suspicious_paths:
        if path in request.path:
            abort(404)
          
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

@app.errorhandler(Exception)
def handle_exception(e):
    # Log the full stack trace
    app.logger.error('Unhandled exception', exc_info=True)   

@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify(error="Rate limit exceeded. Please try again later."), 429

class User(UserMixin):
    def __init__(self, id, name, is_admin):
        self.id = str(id)  # Flask-Login needs string ID
        self.name = name
        self.is_admin = is_admin

    def get_id(self):
        return self.id
 
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
        app.logger.error(f"Failed to get database connection: {str(e)}")
        # If we got a connection but the test failed, return it to the pool
        if 'connection' in locals():
            try:
                db_pool.putconn(connection)
            except Exception as put_err:
                app.logger.error(f"Failed to return failed connection to pool: {str(put_err)}")
        return None

def return_db(conn):
    if conn:
        try:
            db_pool.putconn(conn)
            app.logger.debug("Connection returned to pool successfully")
        except Exception as e:
            app.logger.error(f"Error returning connection to pool: {str(e)}")

def init_db():
    try:
        app.logger.debug("Starting database initialization")
        with get_db_connection() as conn:  # Use the context manager
            if conn is None:
                app.logger.error("Could not establish database connection")
                return False
            
            # This should be inside the first with block
            with conn.cursor() as cursor:
                app.logger.debug("Setting search path")
                cursor.execute('SET search_path TO public')
                
                app.logger.debug("Creating users table")
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
                
                app.logger.debug("Creating parsed_receipts table")
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
                
                app.logger.debug("Checking for admin user")
                cursor.execute("SELECT * FROM users WHERE username = 'admin'")
                admin_exists = cursor.fetchone()
                
                if not admin_exists:
                    app.logger.debug("Creating admin user")
                    admin_password = bcrypt.generate_password_hash('admin123').decode('utf-8')
                    cursor.execute('''
                        INSERT INTO users (name, email, phone, username, password, approved, is_admin)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ''', ('Admin User', 'admin@example.com', '1234567890', 'admin', admin_password, 1, 1))
                
                conn.commit()
                app.logger.info("Database tables and admin user created successfully")
                return True
                
    except Exception as e:
        app.logger.error(f"Database initialization error: {type(e).__name__}")
        app.logger.error(f"Error details: {str(e)}")
        if 'conn' in locals():  # Only try to rollback if we have a connection
            try:
                conn.rollback()
                app.logger.debug("Transaction rolled back successfully")
            except Exception as rollback_error:
                app.logger.error(f"Rollback failed: {str(rollback_error)}")
        return False
      
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
CONNECTION_POOL = None

def init_db_pool(app):
    global CONNECTION_POOL
    try:
        CONNECTION_POOL = SimpleConnectionPool(
            minconn=1,   # Minimum number of connections
            maxconn=20,  # Maximum number of connections
            host=app.config['DB_HOST'],
            database=app.config['DB_NAME'],
            user=app.config['DB_USER'],
            password=app.config['DB_PASSWORD']
        )
        app.logger.info("Database connection pool initialized successfully")
    except Exception as e:
        app.logger.error(f"Error initializing database connection pool: {e}")
        raise
      
def release_db(conn):
    global CONNECTION_POOL
    try:
        if CONNECTION_POOL and conn:
            CONNECTION_POOL.putconn(conn)
    except Exception as e:
        logging.error(f"Error releasing database connection: {e}")
      
def initialize_database():
    try:
        with app.app_context():
            success = init_db()
            if not success:
                app.logger.error("Database initialization failed during startup")
                sys.exit(1)  # Exit if database init fails
            app.logger.info("Database initialized successfully during startup")
    except Exception as e:
        app.logger.error(f"Critical error during database initialization: {str(e)}")
        sys.exit(1)

# Call this function when the app starts
with app.app_context():
    if init_db():
        app.logger.info("Database initialized successfully")
    else:
        app.logger.error("Failed to initialize database")

app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')

if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

handler = RotatingFileHandler('flask_app.log', maxBytes=10000, backupCount=3)
handler.setLevel(logging.ERROR)
app.logger.addHandler(handler)

@login_manager.user_loader
def load_user(user_id):
    conn = None
    try:
        conn = get_db()
        if conn is None:
            logging.error("Could not establish database connection for user loading")
            return None
        
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, name, is_admin FROM users WHERE id = %s", (user_id,))
            user_data = cursor.fetchone()
            if user_data:
                return User(
                    id=user_data[0],
                    name=user_data[1],
                    is_admin=user_data[2] == 1
                )
        return None
    except Exception as e:
        logging.error(f"Error loading user: {e}")
        return None
    finally:
        if conn:
            release_db(conn)
# Redis connection check (separate from user loader)
try:
    limiter.storage.storage.ping()
    app.logger.info("Successfully connected to Redis")
except Exception as e:
    app.logger.error(f"Failed to connect to Redis: {str(e)}")
    from limits.storage import MemoryStorage
    limiter.storage = MemoryStorage()

DATABASE = '/data/users.db'
os.makedirs('/home/ubuntu/SalesPal/data', exist_ok=True)  # Create the directory if it doesn't exist

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
@login_required
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
@login_required
def non_admin_dashboard():
    # Check if the current user is an admin
    if current_user.is_admin:
        return redirect(url_for('admin_home'))
    
    conn = None
    try:
        conn = get_db()
        if conn is None:
            app.logger.error("Could not establish database connection")
            flash('Database connection error', 'error')
            return render_template('error.html'), 500
        
        with conn.cursor() as cursor:
            # Query to get the user's name using the current user's ID
            cursor.execute("SELECT name FROM users WHERE id = %s", (current_user.id,))
            user = cursor.fetchone()
            
            if user:
                # Pass the name to the template
                return render_template('non_admin_dashboard.html', current_user=user[0])
            else:
                # Fallback to username if name not found
                return render_template('non_admin_dashboard.html', current_user=current_user.name)
    
    except Exception as e:
        app.logger.error(f"Database error in non_admin_dashboard: {str(e)}")
        flash('An error occurred while loading dashboard', 'error')
        return render_template('error.html'), 500
    
    finally:
        if conn:
            release_db(conn)

@app.route('/delete_receipt/<int:receipt_id>', methods=['POST'])
@login_required
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
@login_required
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
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        try:
            db = get_db()
            if db is None:
                flash("Database connection error", "error")
                return render_template('login.html')
            
            with db.cursor() as cursor:
                cursor.execute("""
                    SELECT id, name, email, phone, username, password, approved, is_admin 
                    FROM users 
                    WHERE username = %s
                """, (username,))
                user_data = cursor.fetchone()
                
                # Add more detailed logging
                print(f"Username entered: {username}")
                print(f"User data found: {user_data}")
                
                if user_data:
                    is_password_correct = bcrypt.check_password_hash(user_data[5], password)
                    print(f"Stored hash: {user_data[5]}")
                    print(f"Password check: {is_password_correct}")
                    print(f"Approved status: {user_data[6]}")
                    print(f"Is admin: {user_data[7]}")
                    
                    if is_password_correct and user_data[6] == 1:  # Approved
                        user = User(
                            id=user_data[0],  # user ID
                            name=user_data[1],  # assuming name is the second column
                            is_admin=user_data[7] == 1  # convert to boolean
                        )
                        login_user(user)
                        
                        if user.is_admin:
                            print("Redirecting to admin_home")
                            return redirect(url_for('admin_home'))
                        else:
                            print("Redirecting to non_admin_dashboard")
                            return redirect(url_for('non_admin_dashboard'))
                    else:
                        print("Login failed: incorrect password or not approved")
                        flash("Invalid username or password or account not approved", "error")
                else:
                    print("No user found with this username")
                    flash("Invalid username or password", "error")
        except Exception as e:
            print("Unexpected login error:", str(e))
            flash("An unexpected error occurred", "error")
    
    return render_template('login.html')
  
def reset_user_password(username, new_password):
    try:
        with get_db_connection() as db:  # Use context manager
            with db.cursor() as cursor:
                # First check if user exists
                cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
                user = cursor.fetchone()
                
                if not user:
                    app.logger.error(f"Password reset failed: User {username} not found")
                    return False
                
                # Hash the new password
                hashed_password = bcrypt.generate_password_hash(new_password).decode('utf-8')
                
                # Update only the password - don't automatically grant admin privileges
                cursor.execute("""
                    UPDATE users 
                    SET password = %s
                    WHERE username = %s
                    RETURNING id
                """, (hashed_password, username))
                
                updated = cursor.fetchone()
                db.commit()
                
                if updated:
                    app.logger.info(f"Password successfully reset for user: {username}")
                    return True
                else:
                    app.logger.error(f"Password reset failed: No rows updated for {username}")
                    return False
                    
    except Exception as e:
        app.logger.error(f"Password reset error: {str(e)}")
        if 'db' in locals():
            db.rollback()
        return False

# Route with rate limiting
@app.route('/reset_password', methods=['POST'])
@limiter.limit("3 per hour")
@login_required  # If this should only be accessible to logged-in users
def reset_password_route():
    try:
        data = request.get_json()
        username = data.get('username')
        new_password = data.get('new_password')
        
        if not username or not new_password:
            return jsonify({'error': 'Missing username or new password'}), 400
            
        if reset_user_password(username, new_password):
            return jsonify({'message': 'Password reset successful'}), 200
        else:
            return jsonify({'error': 'Password reset failed'}), 400
            
    except Exception as e:
        app.logger.error(f"Password reset route error: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

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
@login_required
def admin_home():
    # Check if the current user is an admin
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.', 'error')
        return redirect(url_for('login'))
        
    conn = None
    try:
        conn = get_db()
        if conn is None:
            app.logger.error("Could not establish database connection")
            flash("Database connection error", "error")
            return render_template('error.html'), 500
            
        with conn.cursor() as cursor:
            # Use current_user.id from Flask-Login
            cursor.execute("SELECT name FROM users WHERE id = %s", (current_user.id,))
            user = cursor.fetchone()
            current_username = user[0] if user else 'User'
            
        return render_template('admin_home.html', current_user=current_username)
        
    except Exception as e:
        app.logger.error(f"Error in admin home: {str(e)}")
        flash("An error occurred while loading admin home", "error")
        return render_template('error.html'), 500
    
    finally:
        if conn:
            release_db(conn)  # Use connection pool release method

# Admin page to list employees and approve/reject accounts
@app.route('/admin/employees')
@login_required
def employee_list():
    # Check if the current user is an admin
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.')
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
            cursor.execute("SELECT name FROM users WHERE id = %s", (current_user.id,))
            user = cursor.fetchone()
            current_username = user[0] if user else 'User'
            
        return render_template('employee_list.html', 
                             employees=employees, 
                             current_user=current_username)
                             
    except Exception as e:
        app.logger.error(f"Error in employee list: {str(e)}")
        return "Error loading employee list", 500
    finally:
        if 'db' in locals():
            db.close()

@app.route('/admin/assign_username/<int:user_id>', methods=['POST'])
@login_required
def assign_username(user_id):
    # Check if the current user is an admin
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.', 'error')
        return redirect(url_for('login'))
    
    username = request.form['username']
    conn = None
    
    try:
        conn = get_db()
        if conn is None:
            flash('Database connection error', 'error')
            return redirect(url_for('employee_list'))
        
        with conn.cursor() as cursor:
            # Check if username already exists
            cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
            existing_user = cursor.fetchone()
            
            if existing_user:
                flash('Username already exists. Please choose another.', 'error')
                return redirect(url_for('employee_list'))
            
            # Update username
            cursor.execute("UPDATE users SET username = %s WHERE id = %s", (username, user_id))
            conn.commit()
            
            flash('Username assigned successfully.', 'success')
            return redirect(url_for('employee_list'))
    
    except Exception as e:
        app.logger.error(f"Error assigning username: {str(e)}")
        flash('An error occurred while assigning username.', 'error')
        return redirect(url_for('employee_list'))
    
    finally:
        if conn:
            release_db(conn)
          
@app.route('/admin/approve/<int:user_id>', methods=['POST'])
@login_required
def approve_account(user_id):
    # Ensure only admins can approve accounts
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.', 'error')
        return redirect(url_for('login'))
    
    try:
        db = get_db()
        cursor = db.cursor()
        
        # Check if the user exists
        cursor.execute("SELECT approved, rejected FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()
        
        if not user:
            flash('User not found.', 'error')
            return redirect(url_for('employee_list'))
        
        # Check if user is already approved
        if user[0] == 1:
            flash('User account is already approved.', 'info')
            return redirect(url_for('employee_list'))
        
        # Update the user's approved status to 1 and clear rejected flag
        cursor.execute("UPDATE users SET approved = 1, rejected = 0 WHERE id = %s", (user_id,))
        db.commit()
        
        flash('User account approved successfully.', 'success')
        return redirect(url_for('employee_list'))
    
    except Exception as e:
        app.logger.error(f"Error approving user account: {str(e)}")
        flash('An error occurred while approving the account.', 'error')
        return redirect(url_for('employee_list'))
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'db' in locals():
            db.close()

@app.route('/admin/reject/<int:user_id>', methods=['POST'])
@login_required
def reject_account(user_id):
    # Check if the current user is an admin
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.', 'error')
        return redirect(url_for('login'))
    
    conn = None
    try:
        conn = get_db()
        if conn is None:
            flash('Database connection error', 'error')
            return redirect(url_for('employee_list'))
        
        with conn.cursor() as cursor:
            # Check if user exists
            cursor.execute("SELECT id FROM users WHERE id = %s", (user_id,))
            if not cursor.fetchone():
                flash('User not found.', 'error')
                return redirect(url_for('employee_list'))
            
            # Set the rejected flag to 1 and approved flag to 0
            cursor.execute("UPDATE users SET rejected = 1, approved = 0 WHERE id = %s", (user_id,))
            conn.commit()
            
            flash('User account rejected successfully.', 'success')
            return redirect(url_for('employee_list'))
    
    except Exception as e:
        app.logger.error(f"Error rejecting user account: {str(e)}")
        flash('An error occurred while rejecting the account.', 'error')
        return redirect(url_for('employee_list'))
    
    finally:
        if conn:
            release_db(conn)

@app.route('/admin/delete/<int:user_id>', methods=['POST'])
@login_required
def delete_account(user_id):
    # Ensure only admins can delete accounts
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.', 'error')
        return redirect(url_for('login'))
    
    try:
        db = get_db()
        cursor = db.cursor()
        
        # Check if the user to be deleted exists and is not an admin
        cursor.execute("SELECT is_admin FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()
        
        if not user:
            flash('User not found.', 'error')
            return redirect(url_for('employee_list'))
        
        if user[0] == 1:  # Prevent deleting other admin accounts
            flash('Cannot delete another admin account.', 'error')
            return redirect(url_for('employee_list'))
        
        # Delete the user by ID
        cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
        db.commit()
        
        flash('User account deleted successfully.', 'success')
        return redirect(url_for('employee_list'))
    
    except Exception as e:
        app.logger.error(f"Error deleting user account: {str(e)}")
        flash('An error occurred while deleting the account.', 'error')
        return redirect(url_for('employee_list'))
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'db' in locals():
            db.close()
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
@login_required
def upload_pdf():
    db = None
    try:
        if request.method == 'GET':
            db = get_db()
            if db is None:
                flash("Database connection error", "error")
                return jsonify({'error': "Database connection error"}), 400

            with db.cursor() as cursor:
                cursor.execute("SELECT name FROM users WHERE id = %s", (current_user.id,))
                user = cursor.fetchone()
                current_user_name = user[0] if user else 'User'

            return render_template('upload.html', current_user=current_user_name)

        # Handle POST (upload)
        if not request.files:
            return jsonify({'success': False, 'message': 'No files uploaded'}), 400

        files = request.files.getlist('pdf[]') if 'pdf[]' in request.files else [request.files['pdf']]
        if not any(file.filename for file in files):
            return jsonify({'success': False, 'message': 'No valid files selected'}), 400

        uploaded_files, errors, parsed_data_list = [], [], []

        for file in files:
            try:
                if file and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    file_content = file.read()
                    if not file_content:
                        raise ValueError("Empty file")

                    reader = PyPDF2.PdfReader(io.BytesIO(file_content))
                    if not reader.pages:
                        raise ValueError("PDF has no pages")

                    pdf_text = "".join(page.extract_text() for page in reader.pages if page.extract_text())
                    if not pdf_text.strip():
                        raise ValueError("PDF contains no text")

                    # Rewind and extract
                    file_stream = io.BytesIO(file_content)
                    result = extract_info_from_pdf(file_stream)

                    # Unpack the result first
                    (company_name, customer, order_date, sales_person, rq_invoice,
                     total_price, accessories_prices, upgrades_count, activations_count,
                     ppp_present, pairs, activation_fee_sum) = result

                    # Now check if required fields are present
                    required_fields = [company_name, customer, order_date, sales_person, rq_invoice]
                    if not all(required_fields):
                        raise ValueError("Missing one or more required fields in PDF")

                    parsed_data_list.append({
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
                    })

                    uploaded_files.append(filename)
                else:
                    raise ValueError("Invalid file format")
            except Exception as e:
                errors.append(f"{file.filename}: {str(e)}")

        if not parsed_data_list:
            return jsonify({'success': False, 'errors': errors}), 400

        session['parsed_data_list'] = parsed_data_list
        session['current_pdf_index'] = 0
        session.modified = True

        flash(f"Successfully processed {len(uploaded_files)} file(s)", "success")
        return redirect(url_for('confirm_receipt'))

    except Exception as e:
        app.logger.error(f"Upload error: {str(e)}")
        flash("Unexpected server error during upload", "error")
        return jsonify({'error': str(e)}), 400

def extract_info_from_pdf(file_stream):
    try:
        # Example extraction logic (modify as per your actual implementation)
        # Here we assume you extract relevant data from the PDF and return it
        # This is just a placeholder logic
        app.logger.debug("Extracting data from PDF")
        
        # Extract the required fields (example placeholders)
        company_name = "Example Company"
        customer = "Customer Name"
        order_date = "2025-02-28"
        sales_person = "Sales Person Name"
        rq_invoice = "12345"
        total_price = 1000.00
        accessories_prices = 150.00
        upgrades_count = 3
        activations_count = 5
        ppp_present = True
        pairs = [{"imei": "1234567890", "iccid": "9876543210"}]
        activation_fee_sum = 50.00

        # Return extracted data
        return [company_name, customer, order_date, sales_person, rq_invoice, 
                total_price, accessories_prices, upgrades_count, activations_count, 
                ppp_present, pairs, activation_fee_sum]
    except Exception as e:
        app.logger.error(f"Error extracting data from PDF: {str(e)}")
        raise ValueError("Error during PDF extraction")

@app.route('/confirm', methods=['GET', 'POST'])
@login_required
def confirm_receipt():
    try:
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

        # Get logged-in user's name
        db = get_db()
        if db is None:
            app.logger.error("Database connection error")
            return "Database connection error", 500

        with db.cursor() as cursor:
            cursor.execute("SELECT name FROM users WHERE id = %s", (current_user.id,))
            user = cursor.fetchone()
            logged_in_user = user[0] if user else 'User'

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
            try:
                with db.cursor() as cursor:
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
                        form_data['activation_fee_sum'], current_user.id, imei_iccid_json
                    ))
                db.commit()
                app.logger.info(f"Successfully inserted data for {form_data['company_name']} from {logged_in_user}")
            except Exception as e:
                app.logger.error(f"Error inserting data into database: {str(e)}")
                db.rollback()
                return jsonify({'error': 'Error saving data to the database'}), 500

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

    except Exception as e:
        app.logger.error(f"Unexpected error in confirm_receipt: {str(e)}")
        return jsonify({'error': 'An unexpected error occurred during confirmation'}), 500

@app.route('/view_receipts')
@login_required
def view_receipts():
    try:
        db = get_db()
        if db is None:
            flash("Database connection error", "error")
            return redirect(url_for('login'))

        with db.cursor() as cursor:
            # Fetch user details in a single query
            cursor.execute("SELECT id, name, is_admin FROM users WHERE id = %s", (current_user.id,))
            user = cursor.fetchone()
            
            if not user:
                flash("User not found", "error")
                return redirect(url_for('login'))

            user_id, current_user_name, is_admin = user
            app.logger.info(f"User: {current_user_name}, Admin: {is_admin}")

            # Fetch receipts based on user role
            if is_admin:
                cursor.execute("""
                    SELECT r.*, u.name AS uploader_name
                    FROM parsed_receipts r
                    LEFT JOIN users u ON r.user_id = u.id
                    ORDER BY r.date_submitted DESC
                """)
            else:
                cursor.execute("""
                    SELECT r.*, u.name AS uploader_name
                    FROM parsed_receipts r
                    LEFT JOIN users u ON r.user_id = u.id
                    WHERE r.user_id = %s
                """, (user_id,))
            
            receipts = cursor.fetchall()

        return render_template('view_receipts.html', receipts=receipts, current_user=current_user_name)

    except Exception as e:
        app.logger.error(f"Error in view_receipts: {str(e)}")
        flash("An unexpected error occurred", "error")
        return redirect(url_for('login'))


@app.route('/receipt_details/<string:rq_invoice>')
@login_required
def receipt_details(rq_invoice):
    if 'logged_in' not in session:
        return redirect(url_for('login'))

    db = get_db()
    cursor = db.cursor()

    # Fetch user details to check if admin
    cursor.execute("SELECT id, name, is_admin FROM users WHERE id = %s", (current_user.id,))
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
        """, (rq_invoice, current_user.id))

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
@login_required
def commission():
    conn = None
    try:
        conn = get_db()
        if conn is None:
            flash('Database connection error', 'error')
            return render_template('error.html'), 500
        
        with conn.cursor() as cursor:
            # Check if user is admin
            cursor.execute("SELECT is_admin FROM users WHERE id = %s", (current_user.id,))
            user = cursor.fetchone()
            is_admin = user and user[0] == 1
            
            if is_admin:
                # Query for all non-admin users
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
                                     is_admin=True,
                                     current_user=current_user.name)
            else:
                # Query for current user
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
                ''', (current_user.id,))
                commission_data = cursor.fetchall()
                
                # Calculate accessories total and progress
                accessories_total = commission_data[0][5] if commission_data else 0
                progress = min((float(accessories_total) / 1750 * 100), 100)
                
                # Get tier from the query result
                current_tier = commission_data[0][6] if commission_data else 1
                
                return render_template('commission.html', 
                                     commission_data=commission_data, 
                                     is_admin=False,
                                     accessories_total=accessories_total,
                                     current_tier=current_tier,
                                     progress=progress,
                                     current_user=current_user.name)
    
    except Exception as e:
        app.logger.error(f"Error in commission route: {str(e)}")
        flash('An error occurred while retrieving commission data', 'error')
        return render_template('error.html'), 500
    
    finally:
        if conn:
            release_db(conn)

if __name__ == '__main__':
    # Import sys if not already imported
    import sys
    
    # Initialize database first
    initialize_database()
    
    # Get port from environment or use default
    port = int(os.environ.get('PORT', 5000))
    
    # Run app with debug mode off in production
    debug_mode = os.environ.get('FLASK_ENV') == 'development'
    app.run(
        host='0.0.0.0', 
        port=port,
        debug=debug_mode  # Only enable debug in development
    )
