from .services import Runner, Service, imported_helper


def local_helper() -> str:
    return "local"


def direct_local() -> str:
    return local_helper()


def imported() -> str:
    return imported_helper()


def typed_receiver(service: Service) -> str:
    return service.direct()


def inherited_receiver(service: Service) -> str:
    return service.inherited()


def interface_receiver(runner: Runner) -> str:
    return runner.run()
