from environs import Env

env = Env()
env.read_env()


class MongoSettings:
    MONGODB_SETTINGS = [
        {
            'db': env.str('MONGODB_DB'),
            'host': env.str('MONGODB_HOST'),
            'username': env.str('MONGODB_USERNAME'),
            'password': env.str('MONGODB_PASSWORD'),
            'alias' : 'default'
        }
    ]


class Config(MongoSettings):
    FLASK_DEBUG = env.bool('FLASK_DEBUG')
    DEBUG = env.bool('FLASK_DEBUG')
    SECRET_KEY = env.str('SECRET_KEY')
