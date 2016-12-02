import random
import logging
from city import City
from celery import Celery
from calendar import monthrange
from flask_socketio import SocketIO
from world.population import load_population #, generate population
from .handlers import SocketsHandler
from app import create_app


def make_celery(app):
    celery = Celery(app.import_name, broker=app.config['CELERY_BROKER_URL'], include=['app.tasks'])
    celery.conf.update(app.config)
    TaskBase = celery.Task
    class ContextTask(TaskBase):
        abstract = True
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return TaskBase.__call__(self, *args, **kwargs)
    celery.Task = ContextTask
    return celery


def socketio():
    return SocketIO(message_queue='redis://localhost:6379')


app = create_app()
app.config.update(
    CELERY_BROKER_URL='redis://localhost:6379',
    CELERY_RESULT_BACKEND='redis://localhost:6379'
)
celery = make_celery(app)


# ehhh hacky
model = None
votes = []
voted = []
players = []
proposal = None
queued_players = []
logger = logging.getLogger('simulation')


@celery.task
def setup_simulation(config):
    """prepare the simulation"""
    global model
    global queued_players
    print('----------------------CONFIG')
    print(config)

    # only setup a new simulation if there
    # is no existing simluation.
    # to start a new simulation, first hit the reset endpoint
    if model is None:
        if not any(isinstance(h, SocketsHandler) for h in logger.handlers):
            # don't redundantly add the handler
            logger.setLevel(logging.INFO)
            sockets_handler = SocketsHandler()
            logger.addHandler(sockets_handler)

        if not any(isinstance(h, logging.FileHandler) for h in logger.handlers):
            file_handler = logging.FileHandler('simulation.log')
            logger.addHandler(file_handler)

        pop = load_population('data/population.json')
        pop = pop[:200] # limit to 200 for now
        model = City(pop, config)

        # send population to the frontend
        s = socketio()
        s.emit('setup', {
            'existing': False,
            'population': [p.as_json() for p in pop],
            'buildings': [{
                'id': b.id,
                'tenants': []
            } for b in model.buildings]
        }, namespace='/simulation')
        s.emit('government', model.government.as_json(), namespace='/simulation')

        # setup queued players
        print('QUEUED PLAYERS')
        print(queued_players)
        for id in queued_players:
            players.append(id)
            person = random.choice([p for p in model.people if p.sid == None])
            person.sid = id
            s.emit('person', person.as_json(), namespace='/player', room=id)
            s.emit('joined', person.as_json(), namespace='/simulation')
        queued_players = []


@celery.task
def add_client(sid):
    # existing simulation, sync new client up
    print('===ADDING CLIENT===')
    s = socketio()
    if model is not None:
        s.emit('setup', {
            'existing': True,
            'population': [p.as_json() for p in model.people],
            'buildings': [{
                'id': b.id,
                'tenants': [{'id': t.id, 'type': type(t).__name__} for t in b.tenants]
            } for b in model.buildings],
            'players': [p.as_json() for p in model.people if p.sid in players]
        }, namespace='/simulation', room=sid)
        s.emit('government', model.government.as_json(), namespace='/simulation')
    else:
        s.emit('init', {
            'queued_players': queued_players
        }, namespace='/simulation', room=sid)


@celery.task
def step_simulation():
    """steps through one month of the simulation"""
    _, n_days = monthrange(model.state['year'], model.state['month'])
    for _ in range(n_days):
        model.step()

    s = socketio()
    s.emit('simulation', {'success': True}, namespace='/simulation')

    # choose a legislation proposer for the next month
    if players:
        print('CHOOSING PROPOSER')
        proposer = random.choice(players)
        s.emit('propose', {'proposals': model.government.proposal_options(model)}, room=proposer, namespace='/player')

    print('---CONFIG------')
    print(model.config)


@celery.task
def choose_proposer():
    global proposal
    if players and proposal is None:
        proposer = random.choice(players)
        s = socketio()
        s.emit('propose', {'proposals': model.government.proposal_options(model)}, room=proposer, namespace='/player')


@celery.task
def start_vote(prop):
    global proposal
    proposal = prop
    socketio().emit('vote', {'proposal': proposal}, namespace='/player')
    socketio().emit('voting', {'proposal': proposal}, namespace='/simulation')
    # end_vote.apply_async(countdown=30)


def check_votes():
    global votes
    global voted
    global proposal
    print('n_votes', len(votes))
    print('n_players', len(players))
    yays = sum(1 if v else 0 for v in votes if v is not None)
    nays = sum(1 if not v else 0 for v in votes if v is not None)
    socketio().emit('votes', {'yays': yays, 'nays': nays}, namespace='/simulation')
    if len(votes) >= len(players) and proposal is not None:
        # vote has concluded
        print('vote done!')
        tally_vote()


@celery.task
def end_vote():
    global votes
    global voted
    global proposal
    if proposal is not None:
        # vote has concluded
        print('vote done! (timeout)')
        tally_vote()

def tally_vote():
    global votes
    global voted
    global proposal
    yay = sum(1 if v else -1 for v in votes if v is not None)
    print('yay votes', yay)
    if yay > 0:
        model.government.apply_proposal(proposal, model)
    s = socketio()
    proposal = None
    votes = []
    voted = []
    s.emit('voted', {'passed': yay > 0}, namespace='/simulation')
    s.emit('government', model.government.as_json(), namespace='/simulation')


@celery.task
def record_vote(vote, sid):
    print('received vote', vote)
    if sid not in voted:
        votes.append(vote)
        voted.append(sid)
    check_votes()


@celery.task
def add_player(id):
    """adds a player to the game, assigning them an unassigned simulant.
    if the game is not ready, they are added to the player queue"""
    s = socketio()
    if model is not None:
        players.append(id)
        person = random.choice([p for p in model.people if p.sid == None])
        person.sid = id
        s.emit('person', person.as_json(), namespace='/player', room=id)
        s.emit('joined', person.as_json(), namespace='/simulation')
    else:
        queued_players.append(id)
        s.emit('joined_queue', {'id': id}, namespace='/simulation')
    print('registered', id)


@celery.task
def remove_player(id):
    """removes a player from the game, releasing their simulant"""
    s = socketio()
    if id in players:
        person = next((p for p in model.people if p.sid == id), None)
        players.remove(id)
        person.sid = None
        s.emit('left', person.as_json(), namespace='/simulation')

        # since player count has changed, re-check votes
        check_votes()
    elif id in queued_players:
        s.emit('left_queue', {'id': id}, namespace='/simulation')
    print('deregistered', id)


@celery.task
def reset():
    """reset the currently-running simulation"""
    global model
    global votes
    global voted
    global proposal
    global players
    global queued_players
    model = None
    proposal = None
    votes = []
    voted = []
    players = []
    queued_players = []
