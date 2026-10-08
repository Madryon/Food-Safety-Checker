import json
import unittest
from app import app
from services.database import (
    register_account, authenticate_account,
    get_user_feedbacks, get_developer_feedbacks, save_feedback_db
)

class FoodCheckPrivateAuthTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.app_context = app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.app_context.pop()

    def test_01_user_registration_and_login(self):
        # Register a unique user
        res = self.client.post('/api/auth/register', json={
            'username': 'alice101',
            'password': 'password123',
            'name': 'Alice Wonder'
        })
        self.assertIn(res.status_code, [200, 400]) # 200 or already registered
        
        # Login
        res_login = self.client.post('/api/auth/login', json={
            'username': 'alice101',
            'password': 'password123'
        })
        self.assertEqual(res_login.status_code, 200)
        data = res_login.get_json()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['user']['user_id'], 'alice101')

    def test_02_auth_me_and_logout(self):
        # Login as user
        with self.client:
            self.client.post('/api/auth/login', json={
                'username': 'alice101',
                'password': 'password123'
            })
            res_me = self.client.get('/api/auth/me')
            self.assertEqual(res_me.status_code, 200)
            data_me = res_me.get_json()
            self.assertTrue(data_me['authenticated'])
            self.assertEqual(data_me['user_id'], 'alice101')

            # Logout
            res_logout = self.client.post('/api/auth/logout')
            self.assertEqual(res_logout.status_code, 200)
            
            # Check me again
            res_me_after = self.client.get('/api/auth/me')
            self.assertFalse(res_me_after.get_json()['authenticated'])

    def test_03_data_isolation_between_users(self):
        # Register Bob
        self.client.post('/api/auth/register', json={
            'username': 'bob202',
            'password': 'password456',
            'name': 'Bob Builder'
        })

        client_alice = app.test_client()
        client_bob = app.test_client()

        # Alice logs in
        client_alice.post('/api/auth/login', json={'username': 'alice101', 'password': 'password123'})
        # Bob logs in
        client_bob.post('/api/auth/login', json={'username': 'bob202', 'password': 'password456'})

        # Alice logs a meal
        res_m_a = client_alice.post('/api/calorie/log', json={
            'meal_type': 'Breakfast',
            'food_item': 'Alice Quinoa Salad',
            'portion': '1 bowl',
            'calories': 300,
            'protein_g': 10,
            'carbs_g': 40,
            'fat_g': 8,
            'log_date': '2026-10-08'
        })
        self.assertEqual(res_m_a.status_code, 200)

        # Bob logs a meal
        res_m_b = client_bob.post('/api/calorie/log', json={
            'meal_type': 'Lunch',
            'food_item': 'Bob Protein Shake',
            'portion': '500ml',
            'calories': 400,
            'protein_g': 35,
            'carbs_g': 20,
            'fat_g': 5,
            'log_date': '2026-10-08'
        })
        self.assertEqual(res_m_b.status_code, 200)

        # Check Alice's daily summary
        res_a_sum = client_alice.get('/api/calorie/today?date=2026-10-08').get_json()
        a_items = [m['food_item'] for m in res_a_sum['meals']]
        self.assertIn('Alice Quinoa Salad', a_items)
        self.assertNotIn('Bob Protein Shake', a_items)

        # Check Bob's daily summary
        res_b_sum = client_bob.get('/api/calorie/today?date=2026-10-08').get_json()
        b_items = [m['food_item'] for m in res_b_sum['meals']]
        self.assertIn('Bob Protein Shake', b_items)
        self.assertNotIn('Alice Quinoa Salad', b_items)

    def test_04_feedback_privacy_and_developer_visibility(self):
        client_alice = app.test_client()
        client_alice.post('/api/auth/login', json={'username': 'alice101', 'password': 'password123'})

        client_bob = app.test_client()
        client_bob.post('/api/auth/login', json={'username': 'bob202', 'password': 'password456'})

        # Register developer account
        self.client.post('/api/auth/register', json={
            'username': 'superdev',
            'password': 'devpass123',
            'name': 'Chief Developer',
            'is_developer': True
        })
        client_dev = app.test_client()
        client_dev.post('/api/auth/login', json={'username': 'superdev', 'password': 'devpass123'})

        # Alice posts feedback
        client_alice.post('/api/feedback', json={
            'name': 'Alice Wonder',
            'rating': 5,
            'message': 'Private review from Alice only!'
        })

        # Bob posts feedback
        client_bob.post('/api/feedback', json={
            'name': 'Bob Builder',
            'rating': 4,
            'message': 'Private review from Bob only!'
        })

        # Alice reads feedbacks
        alice_fbs = client_alice.get('/api/feedback').get_json().get('feedbacks', [])
        alice_msgs = [f['message'] for f in alice_fbs]
        self.assertIn('Private review from Alice only!', alice_msgs)
        self.assertNotIn('Private review from Bob only!', alice_msgs)

        # Bob reads feedbacks
        bob_fbs = client_bob.get('/api/feedback').get_json().get('feedbacks', [])
        bob_msgs = [f['message'] for f in bob_fbs]
        self.assertIn('Private review from Bob only!', bob_msgs)
        self.assertNotIn('Private review from Alice only!', bob_msgs)

        # Developer reads feedbacks
        dev_res = client_dev.get('/api/feedback').get_json()
        self.assertTrue(dev_res.get('is_developer'))
        dev_fbs = dev_res.get('feedbacks', [])
        dev_msgs = [f['message'] for f in dev_fbs]
        self.assertIn('Private review from Alice only!', dev_msgs)
        self.assertIn('Private review from Bob only!', dev_msgs)

    def test_05_meal_suggestion_endpoint(self):
        client_alice = app.test_client()
        client_alice.post('/api/auth/login', json={'username': 'alice101', 'password': 'password123'})

        res = client_alice.post('/api/diet/suggest_meal', json={
            'meal_type': 'Lunch',
            'craving': 'High protein vegetarian',
            'target_calories': 450
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data['status'], 'success')
        self.assertIn('suggestions', data['result'])
        self.assertEqual(len(data['result']['suggestions']), 3)

if __name__ == '__main__':
    unittest.main()
