import datetime
import json
import logging
from contextlib import contextmanager

from sqlalchemy import Boolean
from sqlalchemy import Column
from sqlalchemy import Integer
from sqlalchemy import String

from pajbot.exc import FailedCommand
from pajbot.managers import Base
from pajbot.managers import DBManager
from pajbot.managers import HandlerManager
from pajbot.managers import RedisManager
from pajbot.managers import ScheduleManager
from pajbot.managers import TimeManager
from pajbot.streamhelper import StreamHelper
from pajbot.tbutil import time_method  # NOQA

log = logging.getLogger(__name__)


class User(Base):
    __tablename__ = 'tb_user'

    id = Column(Integer, primary_key=True)
    username = Column(String(128), nullable=False, index=True, unique=True)
    username_raw = Column(String(128))
    level = Column(Integer, nullable=False, default=100)
    points = Column(Integer, nullable=False, default=0)
    subscriber = Column(Boolean, nullable=False, default=False)
    minutes_in_chat_online = Column(Integer, nullable=False, default=0)
    minutes_in_chat_offline = Column(Integer, nullable=False, default=0)

    def __init__(self, username):
        self.id = None
        self.username = username.lower()
        self.username_raw = username
        self.level = 100
        self.points = 0
        self.subscriber = False
        self.minutes_in_chat_online = 0
        self.minutes_in_chat_offline = 0

        self.quest_progress = {}
        self.debts = []

        self.timed_out = False

    @classmethod
    def test_user(cls, username):
        user = cls()

        user.id = 123
        user.username = username.lower()
        user.username_raw = username
        user.level = 2000
        user.subscriber = True
        user.points = 1234
        user.minutes_in_chat_online = 5
        user.minutes_in_chat_offline = 15

        return user


class NoCacheHit(Exception):
    pass


class UserSQLCache:
    cache = {}

    def init():
        ScheduleManager.execute_every(30 * 60, UserSQLCache._clear_cache)

    def _clear_cache():
        UserSQLCache.cache = {}

    def save(user):
        UserSQLCache.cache[user.username] = {
                'id': user.id,
                'level': user.level,
                'subscriber': user.subscriber,
                }

    def get(username, value):
        if username not in UserSQLCache.cache:
            raise NoCacheHit('User not in cache')

        if value not in UserSQLCache.cache[username]:
            raise NoCacheHit('Value not in cache')

        # log.debug('Returning {}:{} from cache'.format(username, value))
        return UserSQLCache.cache[username][value]


class UserSQL:
    def __init__(self, username, db_session, user_model=None):
        self.username = username
        self.user_model = user_model
        self.model_loaded = user_model is not None
        self.shared_db_session = db_session

    def select_or_create(db_session, username):
        user = db_session.query(User).filter_by(username=username).one_or_none()
        if user is None:
            user = User(username)
            db_session.add(user)
        return user

    # @time_method
    def sql_load(self):
        if self.model_loaded:
            return

        self.model_loaded = True

        log.debug('[UserSQL] Loading user model for {}'.format(self.username))
        # from pajbot.tbutil import print_traceback
        # print_traceback()

        if self.shared_db_session:
            user = UserSQL.select_or_create(self.shared_db_session, self.username)
        else:
            with DBManager.create_session_scope(expire_on_commit=False) as db_session:
                user = UserSQL.select_or_create(db_session, self.username)
                db_session.expunge(user)

        self.user_model = user

    def sql_save(self, save_to_db=True):
        if not self.model_loaded:
            return

        if save_to_db and not self.shared_db_session:
            with DBManager.create_session_scope(expire_on_commit=False) as db_session:
                db_session.add(self.user_model)

        UserSQLCache.save(self.user_model)

    @property
    def id(self):
        try:
            return UserSQLCache.get(self.username, 'id')
        except NoCacheHit:
            self.sql_load()
            return self.user_model.id

    @id.setter
    def id(self, value):
        self.sql_load()
        self.user_model.id = value

    @property
    def level(self):
        try:
            return UserSQLCache.get(self.username, 'level')
        except NoCacheHit:
            self.sql_load()
            return self.user_model.level

    @level.setter
    def level(self, value):
        self.sql_load()
        self.user_model.level = value

    @property
    def minutes_in_chat_online(self):
        try:
            return UserSQLCache.get(self.username, 'minutes_in_chat_online')
        except NoCacheHit:
            self.sql_load()
            return self.user_model.minutes_in_chat_online

    @minutes_in_chat_online.setter
    def minutes_in_chat_online(self, value):
        self.sql_load()
        self.user_model.minutes_in_chat_online = value

    @property
    def minutes_in_chat_offline(self):
        try:
            return UserSQLCache.get(self.username, 'minutes_in_chat_offline')
        except NoCacheHit:
            self.sql_load()
            return self.user_model.minutes_in_chat_offline

    @minutes_in_chat_offline.setter
    def minutes_in_chat_offline(self, value):
        self.sql_load()
        self.user_model.minutes_in_chat_offline = value

    @property
    def subscriber(self):
        try:
            return UserSQLCache.get(self.username, 'subscriber')
        except NoCacheHit:
            self.sql_load()
            return self.user_model.subscriber

    @subscriber.setter
    def subscriber(self, value):
        try:
            old_value = UserSQLCache.get(self.username, 'subscriber')
            if old_value == value:
                return
        except NoCacheHit:
            pass

        self.sql_load()
        self.user_model.subscriber = value

    @property
    def points(self):
        self.sql_load()
        return self.user_model.points

    @points.setter
    def points(self, value):
        self.sql_load()
        self.user_model.points = value

    @property
    def duel_stats(self):
        self.sql_load()
        return self.user_model.duel_stats

    @duel_stats.setter
    def duel_stats(self, value):
        self.sql_load()
        self.user_model.duel_stats = value


class UserRedis:
    SS_KEYS = [
            'num_lines',
            ]
    HASH_KEYS = [
            'last_seen',
            'last_active',
            'username_raw',
            ]
    BOOL_KEYS = [
            'ignored',
            'banned',
            ]
    FULL_KEYS = SS_KEYS + HASH_KEYS + BOOL_KEYS

    SS_DEFAULTS = {
            'num_lines': 0,
            }
    HASH_DEFAULTS = {
            'last_seen': None,
            'last_active': None,
            }

    def __init__(self, username):
        self.username = username
        self.redis_loaded = False
        self.values = {}

    def queue_up_redis_calls(self, pipeline):
        streamer = StreamHelper.get_streamer()
        # Queue up calls to the pipeline
        for key in UserRedis.SS_KEYS:
            pipeline.zscore('{streamer}:users:{key}'.format(streamer=streamer, key=key), self.username)
        for key in UserRedis.HASH_KEYS:
            pipeline.hget('{streamer}:users:{key}'.format(streamer=streamer, key=key), self.username)
        for key in UserRedis.BOOL_KEYS:
            pipeline.hget('{streamer}:users:{key}'.format(streamer=streamer, key=key), self.username)

    def load_redis_data(self, data):
        self.redis_loaded = True
        full_keys = list(UserRedis.FULL_KEYS)
        for value in data:
            key = full_keys.pop(0)
            if key in UserRedis.SS_KEYS:
                self.values[key] = self.fix_ss(key, value)
            elif key in UserRedis.HASH_KEYS:
                self.values[key] = self.fix_hash(key, value)
            else:
                self.values[key] = self.fix_bool(key, value)

    # @time_method
    def redis_load(self):
        """ Load data from redis using a newly created pipeline """
        if self.redis_loaded:
            return

        with RedisManager.pipeline_context() as pipeline:
            self.queue_up_redis_calls(pipeline)
            data = pipeline.execute()
            self.load_redis_data(data)

    def fix_ss(self, key, value):
        try:
            val = int(value)
        except:
            val = UserRedis.SS_DEFAULTS[key]
        return val

    def fix_hash(self, key, value):
        if key == 'username_raw':
            val = value or self.username
        else:
            val = value or UserRedis.HASH_DEFAULTS[key]

        return val

    def fix_bool(self, key, value):
        return False if value is None else True

    @property
    def new(self):
        return self._last_seen is None

    @property
    def num_lines(self):
        self.redis_load()
        return self.values['num_lines']

    @num_lines.setter
    def num_lines(self, value):
        # Set cached value
        self.values['num_lines'] = value

        # Set redis value
        if value != 0:
            RedisManager.get().zadd('{streamer}:users:num_lines'.format(streamer=StreamHelper.get_streamer()), self.username, value)
        else:
            RedisManager.get().zrem('{streamer}:users:num_lines'.format(streamer=StreamHelper.get_streamer()), self.username)

    @property
    def _last_seen(self):
        self.redis_load()
        try:
            return datetime.datetime.utcfromtimestamp(float(self.values['last_seen']))
        except:
            return None

    @_last_seen.setter
    def _last_seen(self, value):
        # Set cached value
        value = value.timestamp()
        self.values['last_seen'] = value

        # Set redis value
        RedisManager.get().hset('{streamer}:users:last_seen'.format(streamer=StreamHelper.get_streamer()), self.username, value)

    @property
    def _last_active(self):
        self.redis_load()
        try:
            return datetime.datetime.utcfromtimestamp(float(self.values['last_active']))
        except:
            return None

    @_last_active.setter
    def _last_active(self, value):
        # Set cached value
        value = value.timestamp()
        self.values['last_active'] = value

        # Set redis value
        RedisManager.get().hset('{streamer}:users:last_active'.format(streamer=StreamHelper.get_streamer()), self.username, value)

    @property
    def username_raw(self):
        self.redis_load()
        return self.values['username_raw']

    @username_raw.setter
    def username_raw(self, value):
        # Set cached value
        self.values['username_raw'] = value

        # Set redis value
        if value != self.username:
            RedisManager.get().hset('{streamer}:users:username_raw'.format(streamer=StreamHelper.get_streamer()), self.username, value)
        else:
            RedisManager.get().hdel('{streamer}:users:username_raw'.format(streamer=StreamHelper.get_streamer()), self.username)

    @property
    def ignored(self):
        self.redis_load()
        return self.values['ignored']

    @ignored.setter
    def ignored(self, value):
        # Set cached value
        self.values['ignored'] = value

        if value is True:
            # Set redis value
            RedisManager.get().hset('{streamer}:users:ignored'.format(streamer=StreamHelper.get_streamer()), self.username, 1)
        else:
            RedisManager.get().hdel('{streamer}:users:ignored'.format(streamer=StreamHelper.get_streamer()), self.username)

    @property
    def banned(self):
        self.redis_load()
        return self.values['banned']

    @banned.setter
    def banned(self, value):
        # Set cached value
        self.values['banned'] = value

        if value is True:
            # Set redis value
            RedisManager.get().hset('{streamer}:users:banned'.format(streamer=StreamHelper.get_streamer()), self.username, 1)
        else:
            RedisManager.get().hdel('{streamer}:users:banned'.format(streamer=StreamHelper.get_streamer()), self.username)


class UserCombined(UserRedis, UserSQL):
    """
    A combination of the MySQL Object and the Redis object
    """

    WARNING_SYNTAX = '{prefix}_{username}_warning_{id}'

    def __init__(self, username, db_session=None, user_model=None):
        UserRedis.__init__(self, username)
        UserSQL.__init__(self, username, db_session, user_model=user_model)
        self.username_raw = username

        self.debts = []
        self.moderator = False
        self.timed_out = False
        self.timeout_end = None

    def load(self, **attrs):
        vars(self).update(attrs)

    def save(self, save_to_db=True):
        self.sql_save(save_to_db=save_to_db)
        return {
                'debts': self.debts,
                'moderator': self.moderator,
                'timed_out': self.timed_out,
                'timeout_end': self.timeout_end,
                }

    def get_tags(self, redis=None):
        if redis is None:
            redis = RedisManager.get()
        val = redis.hget('global:usertags', self.username)
        if val:
            return json.loads(val)
        else:
            return {}

    @property
    def last_seen(self):
        ret = TimeManager.localize(self._last_seen)
        return ret

    @last_seen.setter
    def last_seen(self, value):
        self._last_seen = value

    @property
    def last_active(self):
        if self._last_active is None:
            return None
        return TimeManager.localize(self._last_active)

    @last_active.setter
    def last_active(self, value):
        self._last_active = value

    def set_tags(self, value, redis=None):
        if redis is None:
            redis = RedisManager.get()
        return redis.hset('global:usertags', self.username, json.dumps(value, separators=(',', ':')))

    def create_debt(self, points):
        self.debts.append(points)

    def get_warning_keys(self, total_chances, prefix):
        """ Returns a list of keys that are used to store the users warning status in redis.
        Example: ['pajlada_warning1', 'pajlada_warning2'] """
        return [self.WARNING_SYNTAX.format(prefix=prefix, username=self.username, id=id) for id in range(0, total_chances)]

    def get_warnings(self, redis, warning_keys):
        """ Pass through a list of warning keys.
        Example of warning_keys syntax: ['_pajlada_warning1', '_pajlada_warning2']
        Returns a list of values for the warning keys list above.
        Example: [b'1', None]
        Each instance of None in the list means one more Chance
        before a full timeout is in order. """

        return redis.mget(warning_keys)

    def get_chances_used(self, warnings):
        """ Returns a number between 0 and n where n is the amount of
            chances a user has before he should face the full timeout length. """

        return len(warnings) - warnings.count(None)

    def add_warning(self, redis, timeout, warning_keys, warnings):
        """ Returns a number between 0 and n where n is the amount of
            chances a user has before he should face the full timeout length. """

        for id in range(0, len(warning_keys)):
            if warnings[id] is None:
                redis.setex(warning_keys[id], time=timeout, value=1)
                return True

        return False

    def timeout(self, timeout_length, warning_module=None, use_warnings=True):
        """ Returns a tuple with the follow data:
        How long to timeout the user for, and what the punishment string is
        set to.
        The punishment string is used to clarify whether this was a warning or the real deal.
        """

        punishment = 'timed out for {} seconds'.format(timeout_length)

        if use_warnings and warning_module is not None:
            redis = RedisManager.get()

            """ How many chances the user has before receiving a full timeout. """
            total_chances = warning_module.settings['total_chances']

            warning_keys = self.get_warning_keys(total_chances, warning_module.settings['redis_prefix'])
            warnings = self.get_warnings(redis, warning_keys)

            chances_used = self.get_chances_used(warnings)

            if chances_used < total_chances:
                """ The user used up one of his warnings.
                Calculate for how long we should time him out. """
                timeout_length = warning_module.settings['base_timeout'] * (chances_used + 1)
                punishment = 'timed out for {} seconds (warning)'.format(timeout_length)

                self.add_warning(redis, warning_module.settings['length'], warning_keys, warnings)

        return (timeout_length, punishment)

    @contextmanager
    def spend_currency_context(self, points_to_spend, tokens_to_spend):
        # TODO: After the token storage rewrite, use tokens here too
        try:
            self.spend_points(points_to_spend)
            yield
        except FailedCommand:
            log.debug('Returning {} points to {}'.format(points_to_spend, self.username_raw))
            self.points += points_to_spend
        except:
            # An error occured, return the users points!
            log.exception('XXXX')
            log.debug('Returning {} points to {}'.format(points_to_spend, self.username_raw))
            self.points += points_to_spend

    def spend(self, points_to_spend):
        # XXX: Remove all usages of spend() and use spend_points() instead
        return self.spend_points(points_to_spend)

    def spend_points(self, points_to_spend):
        if points_to_spend <= self.points:
            self.points -= points_to_spend
            return True

        return False

    def remove_debt(self, debt):
        try:
            self.debts.remove(debt)
        except ValueError:
            log.error('For some reason the debt {} was not in the list of debts {}'.format(debt, self.debts))

    def pay_debt(self, debt):
        self.points -= debt
        self.remove_debt(debt)

    def points_in_debt(self):
        return sum(self.debts)

    def points_available(self):
        return self.points - self.points_in_debt()

    def can_afford(self, points_to_spend):
        return self.points_available() >= points_to_spend

    def __eq__(self, other):
        return self.username == other.username

    # TODO: rewrite this token code shit
    def can_afford_with_tokens(self, cost):
        num_tokens = self.get_tokens()
        return num_tokens >= cost

    def spend_tokens(self, tokens_to_spend, redis=None):
        if redis is None:
            redis = RedisManager.get()

        user_token_key = '{streamer}:{username}:tokens'.format(
                streamer=StreamHelper.get_streamer(), username=self.username)

        token_dict = redis.hgetall(user_token_key)

        for stream_id in token_dict:
            try:
                num_tokens = int(token_dict[stream_id])
            except (TypeError, ValueError):
                continue

            if num_tokens == 0:
                continue

            decrease_by = min(tokens_to_spend, num_tokens)
            tokens_to_spend -= decrease_by
            num_tokens -= decrease_by

            redis.hset(user_token_key, stream_id, num_tokens)

            if tokens_to_spend == 0:
                return True

        return False

    def award_tokens(self, tokens, redis=None, force=False):
        """ Returns True if tokens were awarded properly.
        Returns False if not.
        Tokens can only be rewarded once per stream ID.
        """

        streamer = StreamHelper.get_streamer()
        stream_id = StreamHelper.get_current_stream_id()

        if stream_id is False:
            return False

        if redis is None:
            redis = RedisManager.get()

        key = '{streamer}:{username}:tokens'.format(
                streamer=streamer, username=self.username)

        if force:
            res = True
            redis.hset(key, stream_id, tokens)
        else:
            res = True if redis.hsetnx(key, stream_id, tokens) == 1 else False
            if res is True:
                HandlerManager.trigger('on_user_gain_tokens', self, tokens)
        return res

    def get_tokens(self, redis=None):
        streamer = StreamHelper.get_streamer()
        if redis is None:
            redis = RedisManager.get()

        tokens = redis.hgetall('{streamer}:{username}:tokens'.format(
            streamer=streamer, username=self.username))

        num_tokens = 0
        for token_value in tokens.values():
            try:
                num_tokens += int(token_value)
            except (TypeError, ValueError):
                log.warn('Invalid value for tokens, user {}'.format(self.username))

        return num_tokens
