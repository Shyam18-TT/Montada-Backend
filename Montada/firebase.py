import firebase_admin
from firebase_admin import credentials

cred = credentials.Certificate("credentials/montada-86ba6-firebase-adminsdk-fbsvc-8df57cd800.json")

firebase_admin.initialize_app(cred)