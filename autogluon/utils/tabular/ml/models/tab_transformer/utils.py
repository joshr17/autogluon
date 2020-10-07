from tqdm import tqdm
from torch.utils.data import Dataset, DataLoader
from autogluon.utils.tabular.ml.models.tab_transformer.TabTransformerEncoder import WontEncodeError, NullEnc
import torch
from autogluon.utils.tabular.ml.models.tab_transformer import TabTransformerEncoder
from autogluon.utils.tabular.ml.models.tab_transformer import pretexts


def augmentation(data, target, mask_prob=0.4, num_augs=1):
    shape=data.shape
    cat_data=torch.cat([data for _ in range(num_augs)])
    target=torch.cat([target for _ in range(num_augs)]).view(-1)
    locs_to_mask = torch.empty_like(cat_data, dtype=float).uniform_() < mask_prob
    cat_data[locs_to_mask] = 0 
    cat_data=cat_data.view(-1,shape[-1])
    return cat_data, target


def epoch(net, data_loader, optimizers, loss_criterion, pretext, state, scheduler, epoch, epochs, aug_kwargs=None):
    is_train = (optimizers is not None)
    net.train() if is_train else net.eval()
    total_loss, total_correct, total_num, data_bar = 0.0, 0.0, 0, tqdm(data_loader)

    if aug_kwargs is None:
        aug_kwargs={'mask_prob': 0.4,
                    'num_augs': 1}


    with (torch.enable_grad() if is_train else torch.no_grad()):
        for data, target in data_bar:
            data, target = pretext.get(data, target)

            # TODO: This could be a user-defined arg instead like 'num_gpus' or 'use_gpu'
            if torch.cuda.is_available():
                data, target = data.cuda(), target.cuda()
                pretext = pretext.cuda()

            if state in [None, 'finetune']:
                data, target = augmentation(data,target, **aug_kwargs)
                out, _    = net(data)
              
            elif state=='pretrain':
                _, out    = net(data)
            else:
                raise NotImplementedError("state must be one of [None, 'pretrain', 'finetune']")
        
            loss, correct  = pretext(out, target)

            if is_train:
                for optimizer in optimizers:
                    optimizer.zero_grad()
                loss.backward()
                for optimizer in optimizers:
                    optimizer.step()

            total_num += 1
            total_loss += loss.item() 

            if epochs==1:
                train_test = 'Test'
            else:
                train_test = 'Train'

            if correct is not None:
                total_correct += correct.mean().cpu().numpy() 
                data_bar.set_description('{} Epoch: [{}/{}] Loss: {:.4f} Acc: {:.2f}%'.format(train_test, epoch, epochs, total_loss / total_num, total_correct / total_num * 100))
            else:
                data_bar.set_description('{} Epoch: [{}/{}] Loss: {:.4f}'.format(train_test, epoch, epochs, total_loss / total_num))
        return total_loss / total_num, total_correct / total_num * 100

    if scheduler is not None:
        scheduler.step()
    return total_loss / total_num


def get_col_info(X):
    cols=list(X.columns)
    col_info=[]
    for c in cols:
        col_info.append({"name": c, "type": "CATEGORICAL"})
    return col_info


class TabTransformerDataset(Dataset):
    def __init__(
        self,
        X,
        y=None,
        col_info=None,
        **kwargs):
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


    def build_loader(self):
        loader = DataLoader(self, batch_size=self.kwargs['batch_size'], 
                            shuffle=False, num_workers=16,
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

