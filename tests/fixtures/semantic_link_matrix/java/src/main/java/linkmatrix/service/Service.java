package linkmatrix.service;

import linkmatrix.api.Runner;

public class Service extends BaseService implements Runner {
    public String direct() {
        return "direct";
    }

    @Override
    public String run() {
        return "run";
    }

    public String overload(int value) {
        return "int:" + value;
    }

    public String overload(String value) {
        return "string:" + value;
    }
}
