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

    def test_04_community_feedback_visibility_and_editing(self):
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
        post_a = client_alice.post('/api/feedback', json={
            'name': 'Alice Wonder',
            'rating': 5,
            'message': 'Initial review from Alice!'
        }).get_json()
        fb_a_id = post_a['feedback']['id']

        # Bob posts feedback
        post_b = client_bob.post('/api/feedback', json={
            'name': 'Bob Builder',
            'rating': 4,
            'message': 'Initial review from Bob!'
        }).get_json()
        fb_b_id = post_b['feedback']['id']

        # 1. Community visibility: Both signed-in users see all feedback
        alice_fbs = client_alice.get('/api/feedback').get_json().get('feedbacks', [])
        alice_msgs = [f['message'] for f in alice_fbs]
        self.assertIn('Initial review from Alice!', alice_msgs)
        self.assertIn('Initial review from Bob!', alice_msgs)

        bob_fbs = client_bob.get('/api/feedback').get_json().get('feedbacks', [])
        bob_msgs = [f['message'] for f in bob_fbs]
        self.assertIn('Initial review from Alice!', bob_msgs)
        self.assertIn('Initial review from Bob!', bob_msgs)

        # 2. Permissions: Alice can edit Alice's, but NOT Bob's
        alice_own_item = next(f for f in alice_fbs if f['id'] == fb_a_id)
        alice_bob_item = next(f for f in alice_fbs if f['id'] == fb_b_id)
        self.assertTrue(alice_own_item.get('can_edit'))
        self.assertFalse(alice_bob_item.get('can_edit'))

        # Bob can edit Bob's, but NOT Alice's
        bob_own_item = next(f for f in bob_fbs if f['id'] == fb_b_id)
        bob_alice_item = next(f for f in bob_fbs if f['id'] == fb_a_id)
        self.assertTrue(bob_own_item.get('can_edit'))
        self.assertFalse(bob_alice_item.get('can_edit'))

        # 3. Alice edits her feedback
        res_edit_a = client_alice.post(f'/api/feedback/{fb_a_id}/edit', json={
            'rating': 5,
            'message': 'Updated review by Alice: loved the calorie tracker!'
        })
        self.assertEqual(res_edit_a.status_code, 200)
        self.assertEqual(res_edit_a.get_json()['status'], 'success')

        # Verify Bob now sees Alice's updated review
        bob_fbs_after = client_bob.get('/api/feedback').get_json().get('feedbacks', [])
        bob_msgs_after = [f['message'] for f in bob_fbs_after]
        self.assertIn('Updated review by Alice: loved the calorie tracker!', bob_msgs_after)

        # 4. Bob tries to edit Alice's feedback -> forbidden (403)
        res_edit_forbidden = client_bob.post(f'/api/feedback/{fb_a_id}/edit', json={
            'rating': 1,
            'message': 'Malicious edit by Bob'
        })
        self.assertEqual(res_edit_forbidden.status_code, 403)

        # 5. Developer can edit any feedback
        res_dev_edit = client_dev.post(f'/api/feedback/{fb_b_id}/edit', json={
            'rating': 5,
            'message': 'Moderated and verified review for Bob!'
        })
        self.assertEqual(res_dev_edit.status_code, 200)

        # 6. Unauthenticated visitor cannot see community feedback without logging in
        client_anon = app.test_client()
        anon_res = client_anon.get('/api/feedback').get_json()
        self.assertTrue(anon_res.get('prompt_login'))

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
