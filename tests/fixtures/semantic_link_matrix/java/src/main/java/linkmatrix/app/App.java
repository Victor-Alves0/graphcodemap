package linkmatrix.app;

import java.util.function.Function;

import linkmatrix.api.Runner;
import linkmatrix.service.BaseService;
import linkmatrix.service.Service;

import static linkmatrix.service.Utility.importedHelper;

public class App {
    private static String localHelper() {
        return "local";
    }

    public String directLocal() {
        return localHelper();
    }

    public String imported() {
        return importedHelper();
    }

    public String typedReceiver(Service service) {
        return service.direct();
    }

    public String inheritedReceiver(Service service) {
        return service.inherited();
    }

    public String interfaceReceiver(Runner runner) {
        return runner.run();
    }

    public String overload(Service service) {
        return service.overload(1);
    }

    public Function<BaseService, String> methodReference() {
        return BaseService::inherited;
    }
}
