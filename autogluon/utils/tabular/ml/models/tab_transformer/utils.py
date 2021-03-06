from autogluon import try_import_torch
from autogluon.utils.tabular.ml.models.tab_transformer.TabTransformerEncoder import WontEncodeError, NullEnc
from autogluon.utils.tabular.ml.models.tab_transformer import TabTransformerEncoder


def augmentation(data, target, mask_prob=0.4, num_augs=1):
    try_import_torch()
    import torch
    shape=data.shape
    cat_data=torch.cat([data for _ in range(num_augs)])
    target=torch.cat([target for _ in range(num_augs)]).view(-1)
    locs_to_mask = torch.empty_like(cat_data, dtype=float).uniform_() < mask_prob
    cat_data[locs_to_mask] = 0 
    cat_data=cat_data.view(-1,shape[-1])
    return cat_data, target

def get_col_info(X):
    cols=list(X.columns)
    col_info=[]
    for c in cols:
        col_info.append({"name": c, "type": "CATEGORICAL"})
    return col_info

class TabTransformerDatasetClass:
    try_import_torch()
    from torch.utils.data import Dataset

    class TabTransformerDataset(Dataset):
        def __init__(
            self,
            X,
            y=None,
            col_info=None,
            **kwargs):
            try_import_torch()
            import torch
            self.encoders = kwargs['encoders']
            self.kwargs   = kwargs
            self.col_info = col_info

            self.raw_data = X

            if y is None:
                self.targets = None
            elif self.kwargs['problem_type']=='regression':
                self.targets = torch.FloatTensor(y)
            else:
                self.targets = torch.LongTensor(y)

            if col_info is None:
                self.columns = get_col_info(X) #this is a stop-gap -- it just sets all feature types to CATEGORICAL.
            else:
                self.columns = self.col_info


            """must be a list of dicts, each dict is of the form {"name": col_name, "type": col_type} 
            where col_name is obtained from the df X, and col_type is CATEGORICAL, TEXT or SCALAR
     
            #TODO FIX THIS self.ds_info['meta']['columns'][1:]
            """
            self.cat_feat_origin_cards = None
            self.cont_feat_origin = None
            self.feature_encoders = None

        @property
        def n_cont_features(self):
            return len(self.cont_feat_origin) if self.encoders is not None else None

        def fit_feat_encoders(self):
            if self.encoders is not None:
                self.feature_encoders = {}
                for c in self.columns:
                    col = self.raw_data[c['name']]
                    enc = TabTransformerEncoder.__dict__[self.encoders[c['type']]]()

                    if c['type'] == 'SCALAR' and col.nunique() < 32:
                        print(f"Column {c['name']} shouldn't be encoded as SCALAR. Switching to CATEGORICAL.")
                        enc = TabTransformerEncoder.__dict__[self.encoders['CATEGORICAL']]()
                    try:
                        enc.fit(col)
                    except WontEncodeError as e:
                        print(f"Not encoding column '{c['name']}': {e}")
                        enc = NullEnc()
                    self.feature_encoders[c['name']] = enc


        def encode(self, feature_encoders):
            try_import_torch()
            import torch
            if self.encoders is not None:
                self.feature_encoders = feature_encoders

                self.cat_feat_origin_cards = []
                cat_features = []
                self.cont_feat_origin = []
                cont_features = []
                for c in self.columns:
                    enc = feature_encoders[c['name']]
                    col = self.raw_data[c['name']]
                    cat_feats = enc.enc_cat(col)
                    if cat_feats is not None:
                        self.cat_feat_origin_cards += [(f'{c["name"]}_{i}_{c["type"]}', card) for i, card in
                                                                                        enumerate(enc.cat_cards)]
                        cat_features.append(cat_feats)
                    cont_feats = enc.enc_cont(col)
                    if cont_feats is not None:
                        self.cont_feat_origin += [c['name']] * enc.cont_dim
                        cont_features.append(cont_feats)
                if cat_features:
                    self.cat_data = torch.cat(cat_features, dim=1)
                else:
                    self.cat_data = None
                if cont_features:
                    self.cont_data = torch.cat(cont_features, dim=1)
                else:
                    self.cont_data = None


        # TODO: Add num_workers as hyperparameter in parameters.py
        def build_loader(self, shuffle=False):
            try_import_torch()
            from torch.utils.data import DataLoader
            loader = DataLoader(self, batch_size=self.kwargs['batch_size'],
                                shuffle=shuffle, num_workers=16,
                                pin_memory=True)

            loader.cat_feat_origin_cards=self.cat_feat_origin_cards
            return loader

        def __len__(self):
            return len(self.raw_data)

        def __getitem__(self, idx):
            target = self.targets[idx] if self.targets is not None else []
            input   = self.cat_data[idx]  if self.cat_data is not None else []
            return input, target

        def data(self):
            return self.raw_data

