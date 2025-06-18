"""Mikrotik Hotspot User Management Backend - v2
Major overhaul with a redesigned UI, profile management, filtered exports, QR codes, and more."""
from flask import Flask, render_template, request, jsonify, send_from_directory, g
from flask_cors import CORS
import librouteros
from librouteros.exceptions import TrapError
import socket
import json
import os
import sys
import logging
import random
import string
import re
import math
import io
import csv
from flask import Response
import base64

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Graceful Dependency Handling ---
# Attempt to import WeasyPrint for PDF export
try:
    from weasyprint import HTML as WeasyHTML
    WEASYPRINT_AVAILABLE = True
    logger.info("WeasyPrint library loaded successfully. PDF export is enabled.")
except (ImportError, OSError) as e:
    WEASYPRINT_AVAILABLE = False
    logger.warning("="*50)
    logger.warning("WeasyPrint could not be loaded. PDF export will be disabled.")
    logger.warning(f"Error: {e}")
    logger.warning("This is likely due to missing GTK+ system dependencies.")
    logger.warning("Please follow the installation steps for your OS:")
    logger.warning("https://doc.weasyprint.org/stable/first_steps.html#installation")
    logger.warning("="*50)


# Attempt to import qrcode for QR generation
try:
    import qrcode
    from qrcode.image.styledpil import StyledPilImage
    from qrcode.image.styles.moduledrawers import RoundedModuleDrawer
    QRCODE_AVAILABLE = True
except ImportError:
    QRCODE_AVAILABLE = False
    logger.warning("qrcode library not found. QR codes on vouchers will be disabled.")
    logger.warning("To enable this feature, please install it: pip install qrcode[pil]")


app = Flask(__name__)
CORS(app)


def get_base_path():
    """ Get base path for PyInstaller bundled app or normal script """
    if hasattr(sys, '_MEIPASS'):
        return sys._MEIPASS
    return os.path.abspath(os.path.dirname(__file__))

class ConfigLoader:
    """Handles loading and managing application configuration."""
    def __init__(self, config_file='config.json'):
        self.config_file = os.path.join(get_base_path(), config_file)
        self.config = self._load_config()

    def _load_config(self):
        """Load configuration from config.json or create default if not exists."""
        default_config = {
            "mikrotik": {
                "host": "192.168.88.1",
                "port": 8728,
                "username": "admin",
                "password": "",
                "use_ssl": False,
                "hotspot_login_url": "http://hotspot.setup/login"
            },
            "server": {
                "host": "0.0.0.0",
                "port": 5000,
                "debug": True
            }
        }

        if os.path.exists(self.config_file):
            with open(self.config_file, 'r') as f:
                loaded_config = json.load(f)
                # Deep merge with default to ensure new keys are present
                default_config['mikrotik'].update(loaded_config.get('mikrotik', {}))
                default_config['server'].update(loaded_config.get('server', {}))
                return default_config
        else:
            with open(self.config_file, 'w') as f:
                json.dump(default_config, f, indent=4)
            return default_config

    def get_config(self):
        return self.config

    def update_config(self, new_config):
        self.config['mikrotik'].update(new_config.get('mikrotik', {}))
        self.config['server'].update(new_config.get('server', {}))
        with open(self.config_file, 'w') as f:
            json.dump(self.config, f, indent=4)

# Initialize ConfigLoader
config_loader = ConfigLoader()
app_config = config_loader.get_config()


def get_mikrotik_api():
    """Establishes and returns a single Mikrotik API connection per request."""
    if 'mikrotik_api' not in g:
        mikrotik_config = app_config['mikrotik']
        host, port, username, password, use_ssl = (
            mikrotik_config['host'], mikrotik_config['port'],
            mikrotik_config['username'], mikrotik_config['password'],
            mikrotik_config.get('use_ssl', False)
        )
        logger.info(f"Attempting to connect to Mikrotik: {host}:{port}")
        try:
            g.mikrotik_connection = librouteros.connect(
                host=host, username=username, password=password, port=port, ssl=use_ssl
            )
            g.mikrotik_api = g.mikrotik_connection
            logger.info("Mikrotik connection established.")
        except (TrapError, socket.error, Exception) as e:
            logger.error(f"Mikrotik connection failed: {e}")
            raise ConnectionError(f"Router connection failed: {e}")
    return g.mikrotik_api

@app.teardown_appcontext
def teardown_connection(exception):
    """Closes the Mikrotik connection after each request."""
    mikrotik_connection = g.pop('mikrotik_connection', None)
    if mikrotik_connection:
        mikrotik_connection.close()
        logger.info("Mikrotik connection closed.")


class RouterOSService:
    """Service class for all Mikrotik RouterOS interactions."""
    def __init__(self):
        pass # Connection is managed globally via get_mikrotik_api

    def test_connection(self) -> tuple[bool, str]:
        """Test connection to Mikrotik router."""
        try:
            api = get_mikrotik_api()
            identity_records = list(api.path('system', 'identity').select('name'))

            router_name = 'Mikrotik Router'
            if identity_records:
                router_name = identity_records[0].get('name', 'Mikrotik Router')

            return True, f"Connected successfully to {router_name}"
        except ConnectionError as e:
            logger.error(f"Connection test failed: {e}")
            return False, f"Connection failed: {e}"
        except Exception as e:
            logger.error(f"Unexpected error during connection test: {str(e)}")
            return False, f"Unexpected error during connection test: {str(e)}"

    def get_hotspot_users(self) -> list:
        """Get all hotspot users."""
        try:
            api = get_mikrotik_api()
            users = list(api.path('ip', 'hotspot', 'user').select(
                '.id', 'name', 'password', 'profile', 'disabled', 'limit-uptime', 'limit-bytes-total',
                'uptime', 'bytes-in', 'bytes-out', 'comment', 'limit-bytes-in', 'limit-bytes-out'
            ))
            return users
        except Exception as e:
            logger.error(f"Error getting users: {str(e)}")
            return []

    def create_hotspot_user(self, user_data: dict) -> tuple[bool, str]:
        """Create new hotspot user."""
        try:
            api = get_mikrotik_api()
            # Clean up potential None values before sending to router
            valid_user_data = {k: v for k, v in user_data.items() if v is not None}
            api.path('ip', 'hotspot', 'user').add(**valid_user_data)
            return True, "User created successfully"
        except (TrapError, Exception) as e:
            logger.error(f"Error creating user: {str(e)}")
            return False, f"Mikrotik Error: {str(e)}"

    def edit_hotspot_user(self, username: str, new_data: dict) -> tuple[bool, str]:
        """Edit existing hotspot user."""
        try:
            api = get_mikrotik_api()
            users = list(api.path('ip', 'hotspot', 'user').select('.id').where(name=username))
            if not users:
                return False, "User not found"
            
            user_id = users[0]['.id']
            api.path('ip', 'hotspot', 'user').set(**new_data, **{'.id': user_id})
            return True, "User updated successfully"
        except (TrapError, Exception) as e:
            logger.error(f"Error editing user '{username}': {str(e)}")
            return False, f"Mikrotik Error: {str(e)}"

    def delete_hotspot_user(self, username: str) -> tuple[bool, str]:
        """Delete hotspot user."""
        try:
            api = get_mikrotik_api()
            users = list(api.path('ip', 'hotspot', 'user').select('.id').where(name=username))
            if not users:
                return False, "User not found"

            user_id = users[0]['.id']
            api.path('ip', 'hotspot', 'user').remove(user_id)
            return True, "User deleted successfully"
        except (TrapError, Exception) as e:
            logger.error(f"Error deleting user '{username}': {str(e)}")
            return False, f"Mikrotik Error: {str(e)}"
    
    def get_active_sessions(self) -> list:
        """Get active hotspot sessions."""
        try:
            api = get_mikrotik_api()
            sessions = list(api.path('ip', 'hotspot', 'active').select(
                'user', 'address', 'mac-address', 'uptime', 'bytes-in', 'bytes-out',
                'session-time-left', 'idle-time', '.id'
            ))
            return sessions
        except Exception as e:
            logger.error(f"Error getting active sessions: {str(e)}")
            return []

    def disconnect_user(self, active_id: str) -> tuple[bool, str]:
        """Disconnect active user session by its .id."""
        try:
            api = get_mikrotik_api()
            api.path('ip', 'hotspot', 'active').remove(active_id)
            return True, "User disconnected successfully"
        except (TrapError, Exception) as e:
            logger.error(f"Error disconnecting user: {str(e)}")
            return False, f"Mikrotik Error: {str(e)}"

    def get_user_profiles(self) -> list:
        """Get hotspot user profiles."""
        try:
            api = get_mikrotik_api()
            profiles = list(api.path('ip', 'hotspot', 'user', 'profile').select(
                '.id', 'name', 'rate-limit', 'session-timeout', 'shared-users',
                'mac-cookie-timeout', 'keepalive-timeout'
            ))
            return profiles
        except Exception as e:
            logger.error(f"Error getting profiles: {str(e)}")
            return []
    
    def create_hotspot_profile(self, profile_data: dict) -> tuple[bool, str]:
        """Creates a new hotspot user profile."""
        try:
            api = get_mikrotik_api()
            data_to_add = {k: v for k, v in profile_data.items() if v}
            api.path('ip', 'hotspot', 'user', 'profile').add(**data_to_add)
            return True, f"Profile '{profile_data['name']}' created successfully."
        except (TrapError, Exception) as e:
            logger.error(f"Error creating profile: {str(e)}")
            return False, f"Mikrotik Error: {str(e)}"

    def edit_hotspot_profile(self, profile_id: str, new_data: dict) -> tuple[bool, str]:
        """Edits an existing hotspot user profile."""
        try:
            api = get_mikrotik_api()
            api.path('ip', 'hotspot', 'user', 'profile').set(**new_data, **{'.id': profile_id})
            return True, "Profile updated successfully."
        except (TrapError, Exception) as e:
            logger.error(f"Error editing profile: {str(e)}")
            return False, f"Mikrotik Error: {str(e)}"

    def delete_hotspot_profile(self, profile_id: str) -> tuple[bool, str]:
        """Deletes a hotspot user profile."""
        try:
            api = get_mikrotik_api()
            api.path('ip', 'hotspot', 'user', 'profile').remove(profile_id)
            return True, "Profile deleted successfully."
        except (TrapError, Exception) as e:
            logger.error(f"Error deleting profile: {str(e)}")
            return False, f"Mikrotik Error: {str(e)}"
    
    def _parse_ros_time(self, time_str: str) -> int:
        """Parses RouterOS time string (e.g., 1w2d3h4m5s) into seconds."""
        if not time_str:
            return 0
        total_seconds = 0
        matches = re.findall(r'(\d+)([wdhms])', time_str)
        for value, unit in matches:
            value = int(value)
            if unit == 'w': total_seconds += value * 604800
            elif unit == 'd': total_seconds += value * 86400
            elif unit == 'h': total_seconds += value * 3600
            elif unit == 'm': total_seconds += value * 60
            elif unit == 's': total_seconds += value
        return total_seconds

    def find_and_delete_expired_users(self) -> tuple[bool, str, int]:
        """Finds and deletes users who have exceeded their time or data limits."""
        try:
            api = get_mikrotik_api()
            users = self.get_hotspot_users()
            deleted_count = 0
            errors = []

            for user in users:
                is_expired = False
                # Check time limit
                if user.get('limit-uptime') and user['limit-uptime'] != '0s':
                    limit_sec = self._parse_ros_time(user['limit-uptime'])
                    usage_sec = self._parse_ros_time(user.get('uptime', '0s'))
                    if limit_sec > 0 and usage_sec >= limit_sec:
                        is_expired = True

                # Check total data limit
                if not is_expired and user.get('limit-bytes-total') and int(user['limit-bytes-total']) > 0:
                    limit_bytes = int(user['limit-bytes-total'])
                    usage_bytes = int(user.get('bytes-in', 0)) + int(user.get('bytes-out', 0))
                    if usage_bytes >= limit_bytes:
                        is_expired = True
                
                if is_expired:
                    try:
                        api.path('ip', 'hotspot', 'user').remove(user['.id'])
                        deleted_count += 1
                        logger.info(f"Deleted expired user '{user['name']}'")
                    except Exception as e:
                        errors.append(user['name'])
                        logger.error(f"Failed to delete expired user '{user['name']}': {e}")
            
            message = f"Successfully deleted {deleted_count} expired user(s)."
            if errors:
                message += f" Failed to delete: {', '.join(errors)}."
            
            return True, message, deleted_count
        except Exception as e:
            logger.error(f"Error during expired user cleanup: {str(e)}")
            return False, f"An unexpected error occurred: {str(e)}", 0

router_os_service = RouterOSService()

# --- Helper Functions ---
def format_bytes_for_export(bytes_val):
    if not bytes_val or bytes_val == '0' or bytes_val == '': return "Unlimited"
    try:
        numeric_bytes = float(bytes_val)
        if numeric_bytes == 0: return "Unlimited"
        k = 1024
        sizes = ['B', 'KB', 'MB', 'GB', 'TB']
        if numeric_bytes < 1: return f"{numeric_bytes:.0f} B"
        i = min(len(sizes) - 1, int(math.floor(math.log(numeric_bytes) / math.log(k))))
        return f"{numeric_bytes / math.pow(k, i):.2f} {sizes[i]}"
    except (ValueError, TypeError):
        return str(bytes_val)

def generate_qr_code_base64(login_url, username, password):
    """Generates a QR code for login and returns a Base64 encoded string."""
    if not QRCODE_AVAILABLE: return None
    
    full_url = f"{login_url}?username={username}&password={password}"
    qr = qrcode.QRCode(version=1, box_size=10, border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(full_url)
    qr.make(fit=True)
    
    img = qr.make_image(image_factory=StyledPilImage, module_drawer=RoundedModuleDrawer())
    
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode('utf-8')

# --- Flask Routes ---
@app.route('/')
def index():
    return send_from_directory(get_base_path(), 'mikrotik_userman_dashboard.html')

@app.route('/api/test-connection', methods=['POST'])
def test_connection():
    success, message = router_os_service.test_connection()
    return jsonify({'success': success, 'message': message})

@app.route('/api/config', methods=['GET'])
def get_config_route():
    cfg = config_loader.get_config()
    # Add status of optional features
    cfg['features'] = {
        'pdf_export': WEASYPRINT_AVAILABLE,
        'qr_codes': QRCODE_AVAILABLE
    }
    return jsonify(cfg)

@app.route('/api/config', methods=['POST'])
def update_config_route():
    data = request.json
    config_loader.update_config(data)
    return jsonify({'success': True, 'message': 'Configuration updated and saved.'})

@app.route('/api/dashboard-stats', methods=['GET'])
def get_dashboard_stats():
    users = router_os_service.get_hotspot_users()
    sessions = router_os_service.get_active_sessions()
    total_users = len(users)
    active_sessions = len(sessions)
    return jsonify({'total_users': total_users, 'active_sessions': active_sessions})

@app.route('/api/users', methods=['GET'])
def get_users():
    users = router_os_service.get_hotspot_users()
    return jsonify({'users': users})

@app.route('/api/users', methods=['POST'])
def create_user():
    data = request.json
    username = data.get('name')
    password = data.get('password')
    if not username or not password:
        return jsonify({'success': False, 'message': 'Username and password are required.'}), 400
    
    success, message = router_os_service.create_hotspot_user(data)
    return jsonify({'success': success, 'message': message})
    
@app.route('/api/bulk-create-users', methods=['POST'])
def bulk_create_users():
    data = request.json
    number_of_users = data.get('number_of_users')
    profile = data.get('profile')
    username_length = int(data.get('username_length', 6))
    password_length = int(data.get('password_length', 8))

    if not all([number_of_users, profile]):
        return jsonify({'success': False, 'message': 'Number of users and profile are required.'}), 400

    base_user_data = {k: v for k, v in data.items() if k not in ['number_of_users', 'username_length', 'password_length'] and v}

    created_credentials = []
    errors = []
    
    for _ in range(int(number_of_users)):
        username = ''.join(random.choices(string.ascii_letters + string.digits, k=username_length))
        password = ''.join(random.choices(string.ascii_letters + string.digits, k=password_length))
        
        user_data = base_user_data.copy()
        user_data['name'] = username
        user_data['password'] = password
        if data.get('comment_prefix'):
            user_data['comment'] = f"{data['comment_prefix']}{username}"

        success, msg = router_os_service.create_hotspot_user(user_data)
        if success:
            created_credentials.append({'username': username, 'password': password})
        else:
            errors.append({'username': username, 'error': msg})

    return jsonify({
        'success': len(errors) == 0,
        'message': f"Created {len(created_credentials)} users. Failed: {len(errors)}.",
        'created_credentials': created_credentials,
        'errors': errors
    })

@app.route('/api/users/<username>', methods=['PUT'])
def edit_user(username: str):
    data = request.json
    if 'disabled' in data:
        data['disabled'] = 'true' if data['disabled'] else 'false'

    success, message = router_os_service.edit_hotspot_user(username, data)
    return jsonify({'success': success, 'message': message})

@app.route('/api/users/<username>', methods=['DELETE'])
def delete_user(username: str):
    success, message = router_os_service.delete_hotspot_user(username)
    return jsonify({'success': success, 'message': message})

@app.route('/api/active-sessions', methods=['GET'])
def get_active_sessions_route():
    sessions = router_os_service.get_active_sessions()
    return jsonify({'sessions': sessions})

@app.route('/api/disconnect-user/<active_id>', methods=['POST'])
def disconnect_user_session(active_id: str):
    success, message = router_os_service.disconnect_user(active_id)
    return jsonify({'success': success, 'message': message})

@app.route('/api/delete-expired-users', methods=['POST'])
def delete_expired_users_route():
    success, message, count = router_os_service.find_and_delete_expired_users()
    return jsonify({'success': success, 'message': message, 'deleted_count': count})

# --- Profile Management Routes ---
@app.route('/api/profiles', methods=['GET'])
def get_profiles_route():
    profiles = router_os_service.get_user_profiles()
    return jsonify({'profiles': profiles})

@app.route('/api/profiles', methods=['POST'])
def create_profile_route():
    data = request.json
    if not data.get('name'):
        return jsonify({'success': False, 'message': 'Profile name is required.'}), 400
    success, message = router_os_service.create_hotspot_profile(data)
    return jsonify({'success': success, 'message': message})

@app.route('/api/profiles/<profile_id>', methods=['PUT'])
def edit_profile_route(profile_id: str):
    data = request.json
    if not data:
        return jsonify({'success': False, 'message': 'No data provided for update.'}), 400
    success, message = router_os_service.edit_hotspot_profile(profile_id, data)
    return jsonify({'success': success, 'message': message})

@app.route('/api/profiles/<profile_id>', methods=['DELETE'])
def delete_profile_route(profile_id: str):
    success, message = router_os_service.delete_hotspot_profile(profile_id)
    return jsonify({'success': success, 'message': message})

# --- MODIFIED EXPORT ROUTE ---
@app.route('/api/export-users', methods=['GET'])
def export_users_route():
    export_format = request.args.get('format', 'json').lower()
    profile_filter = request.args.get('profile_filter')

    users = router_os_service.get_hotspot_users()

    if profile_filter:
        users = [user for user in users if user.get('profile') == profile_filter]

    if not users:
        return "No users found for the selected criteria.", 404

    if export_format == 'json':
        return jsonify(users)

    elif export_format == 'csv':
        si = io.StringIO()
        cw = csv.writer(si)
        headers = [
            'Username', 'Password', 'Profile', 'Time Limit',
            'Total Data Limit', 'Comment', 'Disabled'
        ]
        cw.writerow(headers)
        for user in users:
            row = [
                user.get('name', ''), user.get('password', ''), user.get('profile', ''),
                user.get('limit-uptime', 'Unlimited'),
                format_bytes_for_export(user.get('limit-bytes-total')),
                user.get('comment', ''), user.get('disabled', 'false')
            ]
            cw.writerow(row)
        output = si.getvalue()
        return Response(output, mimetype="text/csv", headers={"Content-disposition": f"attachment; filename=users_{profile_filter or 'all'}.csv"})

    elif export_format == 'html_voucher' or export_format == 'pdf_voucher':
        login_url = app_config['mikrotik'].get('hotspot_login_url', '')
        if not login_url:
            logger.warning("hotspot_login_url not set in config.json. QR Codes will not work.")

        html_content = """
        <html><head><title>Hotspot Vouchers</title>
        <style>
            body { font-family: 'Segoe UI', sans-serif; margin: 10px; background-color: #f4f4f9; }
            .voucher-container { display: grid; grid-template-columns: repeat(auto-fit, minmax(350px, 1fr)); gap: 10px; }
            .voucher { background: white; border: 1px solid #ddd; border-radius: 12px; padding: 15px; page-break-inside: avoid; display: flex; box-shadow: 0 2px 5px rgba(0,0,0,0.05); }
            .voucher-details { flex-grow: 1; }
            .voucher-qr { flex-shrink: 0; width: 120px; height: 120px; margin-left: 15px; }
            .voucher-header { text-align: center; border-bottom: 2px dashed #6a11cb; margin-bottom: 10px; padding-bottom: 5px; }
            .voucher-header h3 { margin: 0; font-size: 1.2em; color: #2575fc; }
            .credentials p { font-size: 1.1em; margin: 8px 0; }
            .credentials strong { color: #333; }
            .credentials span { font-family: 'Courier New', monospace; background: #eee; padding: 3px 6px; border-radius: 4px; color: #d63384; font-weight: bold; }
            .voucher-info { font-size: 0.8em; color: #555; margin-top: 10px; border-top: 1px solid #eee; padding-top: 8px; }
            @media print { body { margin: 0; background: #fff; } .voucher { box-shadow: none; border: 1px dashed #999; } }
        </style></head><body><div class="voucher-container">
        """
        for user in users:
            qr_code_b64 = generate_qr_code_base64(login_url, user.get('name'), user.get('password')) if login_url else None
            html_content += f"""
            <div class="voucher">
                <div class="voucher-details">
                    <div class="voucher-header"><h3>Hotspot Access</h3></div>
                    <div class="credentials">
                        <p><strong>Username:</strong> <span>{user.get('name', 'N/A')}</span></p>
                        <p><strong>Password:</strong> <span>{user.get('password') or 'N/A'}</span></p>
                    </div>
                    <div class="voucher-info">
                        <strong>Profile:</strong> {user.get('profile', 'N/A')} | 
                        <strong>Time Limit:</strong> {user.get('limit-uptime') or 'Unlimited'} | 
                        <strong>Data Limit:</strong> {format_bytes_for_export(user.get('limit-bytes-total'))}
                    </div>
                </div>
                """
            if qr_code_b64:
                html_content += f'<div class="voucher-qr"><img src="data:image/png;base64,{qr_code_b64}" style="width:100%;height:100%;"></div>'
            html_content += "</div>"
        html_content += "</div></body></html>"

        if export_format == 'pdf_voucher':
            if not WEASYPRINT_AVAILABLE:
                return jsonify({"success": False, "message": "PDF generation is disabled. Please install system dependencies for WeasyPrint and restart the application."}), 501
            try:
                pdf_file = WeasyHTML(string=html_content).write_pdf()
                return Response(pdf_file, mimetype="application/pdf", headers={"Content-disposition": f"attachment; filename=vouchers_{profile_filter or 'all'}.pdf"})
            except Exception as e:
                 logger.error(f"Failed to generate PDF: {e}")
                 return jsonify({"success": False, "message": f"An unexpected error occurred during PDF generation: {e}"}), 500
        else: # html_voucher
            return Response(html_content, mimetype="text/html")

    else:
        return jsonify({"success": False, "message": "Invalid export format."}), 400


if __name__ == '__main__':
    server_config = app_config['server']
    print("="*40)
    print("  Mikrotik Hotspot Management System v2")
    print("="*40)
    print(f"\n✅ Dashboard available at: http://{server_config['host']}:{server_config['port']}")
    app.run(host=server_config['host'], port=server_config['port'], debug=server_config['debug'])
